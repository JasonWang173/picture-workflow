import io
import builtins
import json
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from food_compositor.core import compose

import server


def picture(color):
    data = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(data, 'PNG')
    return data.getvalue()


class BatchCompositionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(server, 'BATCH_ROOT', Path(self.directory.name))
        self.executor = ThreadPoolExecutor(max_workers=2)
        def submit(function, *args, estimated_bytes=None, **kwargs):
            return self.executor.submit(function, *args, **kwargs)
        self.pool_patch = patch.object(server, 'submit_composition', side_effect=submit)
        self.worker_patch = patch.object(server, 'worker_count', return_value=2)
        self.worker_patch.start()
        self.root_patch.start()
        self.pool_patch.start()
        self.client = TestClient(server.app)
        self.events = []
        self.compose_patch = patch.object(server, 'compose', self.fake_compose)
        self.compose_patch.start()

    def tearDown(self):
        self.compose_patch.stop()
        self.pool_patch.stop()
        self.worker_patch.stop()
        self.executor.shutdown(wait=True)
        self.root_patch.stop()
        self.directory.cleanup()

    def fake_compose(self, product, template, output, **kwargs):
        template_color = Image.open(template).getpixel((0, 0))
        product_color = Image.open(product).getpixel((0, 0)) if product is not None else (0, 0, 0)
        self.events.append(('start', template_color, product_color, kwargs['mode']))
        if product_color == (0, 255, 0):
            time.sleep(0.03)
        if product_color == (0, 0, 255) and template_color == (255, 255, 0):
            self.events.append(('fail', template_color, product_color))
            raise ValueError('测试失败')
        result = Image.new('RGB', (16, 8), template_color)
        result.paste(product_color, (8, 0, 16, 8))
        result.save(output, 'JPEG', quality=100, subsampling=0)
        self.events.append(('end', template_color, product_color))

    def test_order_pairing_failure_and_selected_zip(self):
        files = [
            ('templates', ('模板一.png', picture((255, 0, 0)), 'image/png')),
            ('templates', ('模板二.png', picture((255, 255, 0)), 'image/png')),
            ('products', ('商品一.png', picture((0, 255, 0)), 'image/png')),
            ('products', ('商品二.png', picture((0, 0, 255)), 'image/png')),
        ]
        for mode in ('cutout', 'overlay'):
            self.events.clear()
            response = self.client.post('/api/compose-batch', files=files, data={'mode': mode, 'size': '16x8'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['total'], 4)
            status = self.client.get(response.json()['status_url']).json()
            self.assertEqual((status['status'], status['done'], status['total']), ('done', 4, 4))
            self.assertEqual([(x['template_index'], x['product_index']) for x in status['images']], [(1, 1), (1, 2), (2, 1)])
            self.assertEqual([(x['template_index'], x['product_index']) for x in status['skipped']], [(2, 2)])
            first_second_template = next(i for i, e in enumerate(self.events) if e[0] == 'start' and e[1] == (255, 255, 0))
            self.assertEqual(sum(e[0] == 'end' and e[1] == (255, 0, 0) for e in self.events[:first_second_template]), 2)
            for item in status['images']:
                image = Image.open(io.BytesIO(self.client.get(item['url']).content))
                expected_template = (255, 0, 0) if item['template_index'] == 1 else (255, 255, 0)
                expected_product = (0, 255, 0) if item['product_index'] == 1 else (0, 0, 255)
                for actual, expected in zip(image.getpixel((2, 2)), expected_template):
                    self.assertLess(abs(actual - expected), 5)
                for actual, expected in zip(image.getpixel((12, 2)), expected_product):
                    self.assertLess(abs(actual - expected), 5)
            selected = status['images'][1]
            archive = self.client.get(status['download_url'], params={'ids': selected['id']})
            self.assertEqual(archive.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                self.assertEqual(zipped.namelist(), [selected['name']])

    def test_three_templates_share_five_products_in_order(self):
        template_colors = [(200, 10, 10), (10, 200, 10), (10, 10, 200)]
        product_colors = [(i * 25, i * 15, i * 10) for i in range(1, 6)]
        files = [('templates', (f'template-{i}.png', picture(color), 'image/png'))
                 for i, color in enumerate(template_colors, 1)]
        files += [('products', (f'product-{i}.png', picture(color), 'image/png'))
                  for i, color in enumerate(product_colors, 1)]
        modes = ['auto', 'cutout', 'overlay']
        response = self.client.post('/api/compose-batch', files=files,
                                    data={'mode': 'auto', 'template_modes': json.dumps(modes)})
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get(response.json()['status_url']).json()
        self.assertEqual((status['status'], status['done'], status['total']), ('done', 15, 15))
        self.assertEqual([(x['template_index'], x['product_index']) for x in status['images']],
                         [(t, p) for t in range(1, 4) for p in range(1, 6)])
        for index, color in enumerate(template_colors):
            starts = [event for event in self.events if event[0] == 'start' and event[1] == color]
            self.assertEqual({event[2] for event in starts}, set(product_colors))
            self.assertEqual({event[3] for event in starts}, {modes[index]})
            if index:
                first_start = next(i for i, event in enumerate(self.events) if event[0] == 'start' and event[1] == color)
                self.assertEqual(sum(event[0] == 'end' and event[1] == template_colors[index - 1]
                                     for event in self.events[:first_start]), 5)
        archive = self.client.get(status['download_url'])
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.namelist(), [image['name'] for image in status['images']])

    def test_rejects_invalid_template_modes(self):
        files = [('templates', ('one.png', picture((255, 0, 0)), 'image/png')),
                 ('products', ('one.png', picture((0, 255, 0)), 'image/png'))]
        for value in ('[]', '["template_replace"]', '{bad'):
            response = self.client.post('/api/compose-batch', files=files,
                                        data={'mode': 'auto', 'template_modes': value})
            self.assertEqual(response.status_code, 400)

    def test_template_replace_processes_each_template_without_products(self):
        files = [('templates', (f'template-{i}.png', picture(color), 'image/png'))
                 for i, color in enumerate([(200, 10, 10), (10, 200, 10), (10, 10, 200)], 1)]
        response = self.client.post('/api/compose-batch', files=files, data={'mode': 'template_replace'})
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get(response.json()['status_url']).json()
        self.assertEqual((status['status'], status['done'], status['total']), ('done', 3, 3))
        self.assertEqual([item['template_index'] for item in status['images']], [1, 2, 3])
        self.assertTrue(all(event[3] == 'template_replace' for event in self.events if event[0] == 'start'))

    def test_legacy_single_template(self):
        files = [('template', ('one.png', picture((255, 0, 0)), 'image/png')),
                 ('products', ('one.png', picture((0, 255, 0)), 'image/png'))]
        response = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(response.json()['status_url']).json()['status'], 'done')


class TemplateLibraryErrorsTest(unittest.TestCase):
    def test_incomplete_template_list_returns_error(self):
        class Failure(Exception):
            pass
        class BrokenBucket:
            def get_object(self, key):
                raise Failure('network error')
        sdk = SimpleNamespace(exceptions=SimpleNamespace(OssError=Failure), ObjectIteratorV2=lambda bucket, prefix: [SimpleNamespace(key='broken.json')])
        with patch.object(server, '_oss_bucket', return_value=BrokenBucket()), patch.object(server, '_oss_sdk', return_value=sdk):
            response = TestClient(server.app).get('/api/templates')
        self.assertEqual(response.status_code, 503)
        self.assertIn('不完整', response.json()['detail'])

    def test_unconfigured_library_returns_useful_error(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(server, '_OSS_BUCKET', None):
            response = TestClient(server.app).get('/api/templates')
        self.assertEqual(response.status_code, 503)
        self.assertIn('OSS_BUCKET', response.json()['detail'])

    def test_blocked_native_dependency_returns_useful_error(self):
        original_import = builtins.__import__
        def blocked_import(name, *args, **kwargs):
            if name == 'oss2':
                raise OSError('application control blocked module')
            return original_import(name, *args, **kwargs)
        environment = {key: 'configured' for key in ('OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_ENDPOINT', 'OSS_BUCKET')}
        environment['OSS_TRANSPORT'] = 'sdk'
        with patch.dict('os.environ', environment), patch.object(server, '_OSS_BUCKET', None), patch('builtins.__import__', side_effect=blocked_import):
            response = TestClient(server.app).get('/api/templates')
        self.assertEqual(response.status_code, 503)
        self.assertIn('Windows', response.json()['detail'])


class AutomaticTemplateModeTest(unittest.TestCase):
    def test_transparent_and_green_slot_templates_choose_different_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (200, 200), 'blue').save(root / 'product.png')
            overlay = Image.new('RGBA', (200, 200), (0, 0, 0, 0))
            ImageDraw.Draw(overlay).rectangle((0, 0, 199, 25), fill=(255, 0, 0, 255))
            overlay.save(root / 'overlay.png')
            cutout = Image.new('RGB', (200, 200), (200, 50, 50))
            ImageDraw.Draw(cutout).rectangle((25, 40, 174, 165), fill=(0, 255, 0))
            cutout.save(root / 'cutout.png')
            for name in ('overlay', 'cutout'):
                with self.subTest(template=name), \
                     patch('food_compositor.core._apply_overlay', return_value=Image.new('RGB', (200, 200))) as overlay_call, \
                     patch('food_compositor.core._place_cutout', return_value=Image.new('RGB', (200, 200))) as cutout_call:
                    compose(root / 'product.png', root / f'{name}.png', root / f'{name}.jpg', mode='auto', size='200x200')
                    self.assertEqual(overlay_call.call_count, int(name == 'overlay'))
                    self.assertEqual(cutout_call.call_count, int(name == 'cutout'))


if __name__ == '__main__':
    unittest.main()
