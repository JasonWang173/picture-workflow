import io
import builtins
import json
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from food_compositor.core import compose
from food_compositor import oss_rest

import server


def picture(color):
    data = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(data, 'PNG')
    return data.getvalue()


def download_filename(response):
    return unquote(response.headers['Content-Disposition'].split("''", 1)[1])


class BatchCompositionTest(unittest.TestCase):
    def setUp(self):
        server.app.middleware_stack = None  # each test starts a fresh request-admission window
        self.directory = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(server, 'BATCH_ROOT', Path(self.directory.name))
        self.executor = ThreadPoolExecutor(max_workers=2)
        def submit(function, *args, estimated_bytes=None, cancel_check=None, **kwargs):
            if cancel_check is not None:
                cancel_check()
            return self.executor.submit(function, *args, **kwargs)
        self.pool_patch = patch.object(server, 'submit_composition', side_effect=submit)
        self.worker_patch = patch.object(server, 'worker_count', return_value=2)
        self.worker_patch.start()
        self.root_patch.start()
        self.pool_patch.start()
        self.client = TestClient(server.app)
        self.archive_objects = {}
        self.oss_failure = False
        def get_object(key):
            if key not in self.archive_objects:
                raise oss_rest.NoSuchKey(key)
            return io.BytesIO(self.archive_objects[key])
        def put_object(key, data):
            if self.oss_failure:
                raise oss_rest.OssError("test outage")
            self.archive_objects[key] = data.encode() if isinstance(data, str) else data
        bucket = SimpleNamespace(
            get_object=get_object, put_object=put_object,
            put_object_from_file=lambda key, path: put_object(key, Path(path).read_bytes()),
            delete_object=lambda key: self.archive_objects.pop(key, None),
        )
        sdk = SimpleNamespace(
            exceptions=oss_rest.exceptions,
            ObjectIteratorV2=lambda bucket, prefix: [
                SimpleNamespace(key=key) for key in sorted(self.archive_objects) if key.startswith(prefix)
            ],
        )
        for mock in (patch.object(server, '_oss_bucket', return_value=bucket),
                     patch.object(server, '_oss_sdk', return_value=sdk)):
            mock.start()
            self.addCleanup(mock.stop)
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
            folder = download_filename(archive).removesuffix('.zip')
            with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                self.assertEqual(zipped.namelist(), [f"{folder}/{selected['name']}"])

    def test_output_filename_uses_beijing_date_and_keeps_sequence_on_download(self):
        files = [
            ('templates', ('模板一.png', picture('red'), 'image/png')),
            ('templates', ('模板二.png', picture('teal'), 'image/png')),
            ('products', ('干锅鸡腿 #2.png', picture((0, 255, 0)), 'image/png')),
            ('products', ('红烧肉.png', picture('blue'), 'image/png')),
        ]
        with patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 22, 15, 59, tzinfo=timezone.utc)
            before_midnight = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'}).json()
            first = self.client.get(before_midnight['status_url']).json()['images'][0]
            self.assertEqual(first['name'], '20261122-001-干锅鸡腿 #2.jpg')

            clock.now.return_value = datetime(2026, 11, 22, 16, 1, tzinfo=timezone.utc)
            after_midnight = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'}).json()
        status = self.client.get(after_midnight['status_url']).json()
        expected = [
            '20261123-001-干锅鸡腿 #2.jpg',
            '20261123-002-红烧肉.jpg',
            '20261123-003-干锅鸡腿 #2.jpg',
            '20261123-004-红烧肉.jpg',
        ]
        self.assertEqual([image['name'] for image in status['images']], expected)
        self.assertEqual(self.client.get(status['images'][0]['url']).status_code, 200)
        image = status['images'][2]
        batch_id = after_midnight['batch_id']
        downloaded = self.client.get(f"/api/batches/{batch_id}/images/{image['id']}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(unquote(downloaded.headers['Content-Disposition'].split("''", 1)[1]), expected[2])
        record = json.loads(unquote(downloaded.headers['X-Composition-Record']))
        archived = self.client.get(f"/api/composition-archive/{record['id']}/image", params={'download': True})
        self.assertEqual(unquote(archived.headers['Content-Disposition'].split("''", 1)[1]), expected[2])
        archive = self.client.get(status['download_url'])
        folder = download_filename(archive).removesuffix('.zip')
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.namelist(), [f'{folder}/{name}' for name in expected])
        self.assertEqual(self.client.get(before_midnight['status_url']).json()['images'][0]['name'], first['name'])

    def test_single_composition_download_uses_dish_filename(self):
        files = {
            'template': ('模板.png', picture('red'), 'image/png'),
            'product': ('香辣鱼:大份.png', picture('blue'), 'image/png'),
        }
        with patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
            result = self.client.post('/api/compose', files=files, data={'mode': 'overlay'})
        self.assertEqual(result.status_code, 200, result.text if not result.is_success else '')
        self.assertEqual(unquote(result.headers['Content-Disposition'].split("''", 1)[1]), '20261123-001-香辣鱼_大份.jpg')

    def test_account_names_follow_actual_dimensions_and_survive_account_switch(self):
        client = TestClient(server.app, base_url='https://workbench.test')
        self.addCleanup(client.close)
        files = [('templates', (name + '.png', picture(color), 'image/png'))
                 for name, color in [('a', 'red'), ('b', 'teal')]]
        files += [('products', (name + '.png', picture(color), 'image/png'))
                  for name, color in [('first', 'green'), ('missing', 'blue'), ('last', 'white')]]

        def mixed_output(product, template, output, **kwargs):
            with Image.open(product) as image:
                color = image.getpixel((0, 0))
            if color == (0, 0, 255):
                raise ValueError('跳过测试商品')
            if color == (0, 128, 0):
                time.sleep(.04)
            with Image.open(template) as image:
                size = (1080, 1920) if image.getpixel((0, 0)) == (255, 0, 0) else (1280, 720)
            Image.new('RGB', size, color).save(output)

        with patch.object(server.account_store, 'path', Path(self.directory.name) / 'accounts.sqlite3'), \
                patch.object(server, 'compose', side_effect=mixed_output), patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 22, 16, 1, tzinfo=timezone.utc)
            password = 'fixture password for initials'
            self.assertEqual(client.post('/api/auth/register', json={'username': 'ZS', 'password': password}).status_code, 201)
            reserved = client.post('/api/batches/reserve').json()['batch_id']
            self.assertEqual(client.post('/api/auth/register', json={'username': 'ww', 'password': password}).status_code, 201)
            result = client.post('/api/compose-batch', files=files, data={'batch_id': reserved, 'size': '1080x1920'})
            self.assertEqual(result.status_code, 200, result.text)
            status = client.get(result.json()['status_url']).json()
            expected = [f'20261123-zs-{orientation}-（{index}）.jpg'
                        for orientation in ('竖版', '横板') for index in (1, 2)]
            self.assertEqual([img['name'] for img in status['images']], expected)
            self.assertEqual([img['product_index'] for img in status['images']], [1, 3, 1, 3])
            single = client.get(f"/api/batches/{reserved}/images/{status['images'][0]['id']}/download")
            self.assertEqual(download_filename(single), expected[0])
            prepared = client.post(status['download_url']).json()
            folder = '2026-11-23-zs（1）'
            self.assertEqual(prepared['filename'], folder + '.zip')
            self.assertTrue(all(item['download_folder'] == folder for item in prepared['items']))
            with zipfile.ZipFile(io.BytesIO(client.get(prepared['url']).content)) as zipped:
                self.assertEqual(zipped.namelist(), [folder + '/' + name for name in expected])
            endpoint = '/api/composition-archive'
            params = {'view': 'page', 'folder': folder, 'limit': 2}
            first = client.get(endpoint, params=params).json()
            second = client.get(endpoint, params={**params, 'cursor': first['next_cursor']}).json()
            self.assertEqual([r['name'] for r in first['items'] + second['items']], expected)
            ref = endpoint + '/' + first['items'][0]['archive_key']
            moved = client.delete(ref).json()['item']
            self.assertEqual((moved['name'], moved['download_folder']), (expected[0], folder))
            self.assertEqual(client.post(ref + '/restore').json()['item']['name'], expected[0])
            self.assertEqual(download_filename(client.get(ref + '/image?download=true')), expected[0])
            for username, number in [('ww', 1), ('zs', 2)]:
                client.post('/api/auth/login', json={'username': username, 'password': password}).raise_for_status()
                new = client.post('/api/compose-batch', files=[files[0], files[2]]).json()
                self.assertEqual(client.post(f"/api/batches/{new['batch_id']}/download").json()['filename'],
                                 f'2026-11-23-{username}（{number}）.zip')
            clock.now.return_value = datetime(2026, 11, 23, 16, 1, tzinfo=timezone.utc)
            self.assertEqual(client.post(status['download_url']).json()['filename'], folder + '.zip')
            next_day = client.post('/api/compose-batch', files=[files[0], files[2]]).json()
            self.assertEqual(client.post(f"/api/batches/{next_day['batch_id']}/download").json()['filename'],
                             '2026-11-24-zs（1）.zip')

    def test_single_account_download_uses_rendered_canvas(self):
        client = TestClient(server.app, base_url='https://workbench.test')
        self.addCleanup(client.close)
        files = {'template': ('poster.png', picture('red'), 'image/png'), 'product': ('dish.png', picture('green'), 'image/png')}
        with patch.object(server.account_store, 'path', Path(self.directory.name) / 'accounts.sqlite3'), \
                patch.object(server, 'compose', compose), patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 22, 16, 1, tzinfo=timezone.utc)
            client.post('/api/auth/register', json={'username': 'zs', 'password': 'fixture password for initials'}).raise_for_status()
            for width, height, orientation in [(1080, 1920, '竖版'), (1280, 720, '横板'), (96, 96, '方版')]:
                response = client.post('/api/compose', files=files, data={'size': f'{width}x{height}', 'mode': 'overlay'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(download_filename(response), f'20261123-zs-{orientation}-（1）.jpg')
                with Image.open(io.BytesIO(response.content)) as image:
                    self.assertEqual(image.size, (width, height))

    def test_downloads_share_numbered_beijing_folders_with_the_archive(self):
        files = [
            ('templates', ('模板.png', picture('red'), 'image/png')),
            ('products', ('栗子鸡.png', picture('blue'), 'image/png')),
        ]
        first, second, third = [self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'}).json()
                                for _ in range(3)]
        statuses = [self.client.get(batch['status_url']).json() for batch in (first, second, third)]
        with patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 23, 15, 59, tzinfo=timezone.utc)
            prepared = self.client.post(statuses[0]['download_url']).json()
            self.assertEqual(prepared['filename'], '2026-11-23（1）.zip')
            self.assertEqual(prepared['items'][0]['download_folder'], '2026-11-23（1）')
            zipped_response = self.client.get(prepared['url'])
            self.assertEqual(download_filename(zipped_response), prepared['filename'])
            with zipfile.ZipFile(io.BytesIO(zipped_response.content)) as zip_file:
                self.assertEqual(zip_file.namelist(), [f"2026-11-23（1）/{statuses[0]['images'][0]['name']}"])

            image_id = statuses[1]['images'][0]['id']
            second_image = self.client.get(f"/api/batches/{second['batch_id']}/images/{image_id}/download")
            record = json.loads(unquote(second_image.headers['X-Composition-Record']))
            self.assertEqual(record['download_folder'], '2026-11-23（2）')
            self.assertEqual(self.client.post(statuses[1]['download_url']).json()['filename'], '2026-11-23（2）.zip')

            clock.now.return_value = datetime(2026, 11, 23, 16, 1, tzinfo=timezone.utc)
            self.assertEqual(self.client.post(statuses[0]['download_url']).json()['filename'], '2026-11-23（1）.zip')
            self.assertEqual(self.client.post(statuses[2]['download_url']).json()['filename'], '2026-11-24（1）.zip')
        groups = self.client.get('/api/composition-archive', params={'view': 'batch_folders'}).json()['folders']
        self.assertEqual([(g['folder'], g['day'], g['count']) for g in groups], [
            ('2026-11-24（1）', '2026-11-24', 1),
            ('2026-11-23（2）', '2026-11-23', 1),
            ('2026-11-23（1）', '2026-11-23', 1),
        ])
        page = self.client.get('/api/composition-archive', params={'view': 'page', 'folder': '2026-11-23（2）'}).json()
        self.assertEqual([image['id'] for image in page['items']], [record['id']])

    def test_trash_before_download_uses_the_same_folder_and_image_name_as_the_batch(self):
        files = [
            ('templates', ('模板.png', picture('red'), 'image/png')),
            ('products', ('栗子鸡.png', picture('blue'), 'image/png')),
            ('products', ('红烧肉.png', picture('green'), 'image/png')),
        ]
        endpoint = '/api/composition-archive'
        with patch.object(server, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc)
            batch = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'}).json()
            status = self.client.get(batch['status_url']).json()
            first, second = status['images']
            deletion = self.client.delete(f"/api/batches/{batch['batch_id']}/images/{first['id']}")
            self.assertEqual(deletion.status_code, 200, deletion.text)
            trashed = deletion.json()['item']
            self.assertEqual((trashed['name'], trashed['download_folder']), ('20261123-001-栗子鸡.jpg', '2026-11-23（1）'))

            zip_download = self.client.get(status['download_url'])
        self.assertEqual(download_filename(zip_download), '2026-11-23（1）.zip')
        with zipfile.ZipFile(io.BytesIO(zip_download.content)) as zipped:
            self.assertEqual(zipped.namelist(), [f"2026-11-23（1）/{second['name']}"])
        for section in ('history', 'trash'):
            folders = self.client.get(endpoint, params={'view': 'batch_folders', 'section': section}).json()
            group, = folders['folders']
            self.assertEqual((group['folder'], group['day'], group['count']), ('2026-11-23（1）', '2026-11-23', 1))
        trash_query = {'view': 'page', 'section': 'trash', 'folder': '2026-11-23（1）'}
        self.assertEqual(self.client.get(endpoint, params=trash_query).json()['items'], [trashed])

        restored = self.client.post(f"{endpoint}/{trashed['id']}/restore").json()['item']
        self.assertEqual((restored['download_folder'], restored['name']), (trashed['download_folder'], trashed['name']))
        self.assertEqual(self.client.get(endpoint, params=trash_query).json()['items'], [])
        outgoing = self.client.get(f"{endpoint}/{trashed['id']}/image", params={'download': True})
        self.assertEqual(download_filename(outgoing), trashed['name'])
        moved = self.client.delete(f"{endpoint}/{trashed['id']}").json()['item']
        self.assertEqual((moved['download_folder'], moved['name']), (trashed['download_folder'], trashed['name']))

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
        folder = download_filename(archive).removesuffix('.zip')
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.namelist(), [f"{folder}/{image['name']}" for image in status['images']])

    def test_rejects_invalid_template_modes(self):
        files = [('templates', ('one.png', picture((255, 0, 0)), 'image/png')),
                 ('products', ('one.png', picture((0, 255, 0)), 'image/png'))]
        for value in ('[]', '["template_replace"]', '{bad'):
            response = self.client.post('/api/compose-batch', files=files,
                                        data={'mode': 'auto', 'template_modes': value})
            self.assertEqual(response.status_code, 400)

    def test_thousand_products_pass_parser_summary_and_json_selected_download(self):
        data = picture((2, 3, 4))
        files = [('templates', ('template.png', picture('red'), 'image/png'))]
        files += [('products', (f'product-{i}.png', data, 'image/png')) for i in range(1000)]
        response = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'})
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()
        status = self.client.get(batch['status_url'], params={'summary': 'true'}).json()
        self.assertEqual((status['status'], status['done'], len(status['images'])), ('done', 1000, 1000))
        self.assertEqual([image['product_index'] for image in status['images']], list(range(1, 1001)))
        self.assertEqual([image['name'] for image in status['images']], sorted(image['name'] for image in status['images']))
        directory = Path(self.directory.name) / batch['batch_id']
        pending = {**server._read_batch_status(directory), 'status': 'processing'}
        server._write_batch_status(directory, pending)
        self.assertNotIn('images', self.client.get(batch['status_url']+'?summary=true').json())
        self.assertEqual(len(self.client.get(batch['status_url']).json()['images']), 1000)
        server._write_batch_status(directory, {**pending, 'status': 'done'})
        prepared = self.client.post(status['download_url'], json={'ids': [item['id'] for item in status['images']]})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        with zipfile.ZipFile(io.BytesIO(self.client.get(prepared.json()['url']).content)) as zipped:
            self.assertEqual(len(zipped.namelist()), 1000)
        self.assertEqual(len(self.archive_objects), 2000)

    def test_low_disk_during_zip_preserves_original_results(self):
        from food_compositor.storage_guard import StorageFull
        files = [('templates', ('template.png', picture('red'), 'image/png')),
                 ('products', ('product.png', picture('blue'), 'image/png'))]
        batch = self.client.post('/api/compose-batch', files=files).json()
        status = self.client.get(batch['status_url']).json()
        with patch.object(server, 'ensure_space', side_effect=StorageFull('磁盘不足')):
            response = self.client.post(status['download_url'], json={'ids': [status['images'][0]['id']]})
        self.assertEqual(response.status_code, 507)
        self.assertEqual(self.client.get(status['images'][0]['url']).status_code, 200)
        self.assertEqual(list((Path(self.directory.name)/batch['batch_id']).glob('*.zip')), [])

    def test_template_replace_processes_each_template_without_products(self):
        files = [('templates', (f'template-{i}.png', picture(color), 'image/png'))
                 for i, color in enumerate([(200, 10, 10), (10, 200, 10), (10, 10, 200)], 1)]
        response = self.client.post('/api/compose-batch', files=files, data={'mode': 'template_replace'})
        self.assertEqual(response.status_code, 200, response.text)
        status = self.client.get(response.json()['status_url']).json()
        self.assertEqual((status['status'], status['done'], status['total']), ('done', 3, 3))
        self.assertEqual([item['template_index'] for item in status['images']], [1, 2, 3])
        self.assertTrue(all(event[3] == 'template_replace' for event in self.events if event[0] == 'start'))

    def test_single_manual_template_uses_async_task_and_preserves_slot(self):
        files = [('templates', ('one.png', picture((255, 0, 0)), 'image/png'))]
        with patch.object(server, 'compose', side_effect=self.fake_compose) as composer:
            response = self.client.post('/api/compose-batch', files=files, data={'mode': 'template_replace', 'slot': '.1,.2,.7,.6'})
            self.assertEqual(response.status_code, 200, response.text)
            status = self.client.get(response.json()['status_url']).json()
            self.assertEqual((status['status'], status['total'], status['done']), ('done', 1, 1))
            self.assertEqual(composer.call_args.kwargs['manual_slot'], (.1, .2, .7, .6))
        response = self.client.post('/api/compose-batch', files=files*2, data={'mode': 'template_replace', 'slot': '.1,.2,.7,.6'})
        self.assertEqual(response.status_code, 400)

    def test_legacy_single_template(self):
        files = [('template', ('one.png', picture((255, 0, 0)), 'image/png')),
                 ('products', ('one.png', picture((0, 255, 0)), 'image/png'))]
        response = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(response.json()['status_url']).json()['status'], 'done')

    def test_registered_members_share_legacy_and_new_records_and_templates(self):
        site = 'https://workbench.test'
        alice, bob = TestClient(server.app, base_url=site), TestClient(server.app, base_url=site)
        self.addCleanup(alice.close)
        self.addCleanup(bob.close)
        archive = '/api/composition-archive'
        with patch.object(server.account_store, 'path', Path(self.directory.name) / 'accounts.sqlite3'):
            def download_batch(client):
                files = [('templates', ('poster.png', picture('red'), 'image/png')),
                         ('products', ('dish.png', picture('green'), 'image/png'))]
                result = client.post('/api/compose-batch', data={'mode': 'overlay'}, files=files).json()
                status = client.get(result['status_url']).json()
                return client.post(status['download_url']).json(), result['status_url']

            legacy, _ = download_batch(self.client)
            for member, name in [(alice, 'alice'), (bob, 'bob')]:
                response = member.post('/api/auth/register', json={'username': name, 'password': 'shared archive test fixture'})
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(member.get(archive, params={'scope': 'team'}).json()['items'][0]['id'], legacy['items'][0]['id'])
            new, active_batch = download_batch(alice)
            self.assertNotEqual(new['filename'], legacy['filename'])
            folders = bob.get(archive, params={'view': 'batch_folders', 'scope': 'team'}).json()['folders']
            group = next(group for group in folders if group['folder'] == new['items'][0]['download_folder'])
            page = bob.get(archive, params={'view': 'page', 'scope': 'team', 'folder': group['folder'], 'batch': group['batch']}).json()
            self.assertEqual([item['id'] for item in page['items']], [new['items'][0]['id']])
            ref = f"{archive}/{page['items'][0]['archive_key']}"
            original = alice.get(ref + '/image')
            self.assertEqual(original.status_code, 200)
            self.assertEqual(bob.get(ref + '/image?download=true').content, original.content)
            self.assertEqual(bob.delete(ref).status_code, 200)
            trash = alice.get(archive, params={'view': 'page', 'section': 'trash'}).json()['items']
            self.assertEqual([item['id'] for item in trash], [new['items'][0]['id']])
            self.assertEqual(alice.post(ref + '/restore').status_code, 200)
            self.assertEqual(bob.get(ref + '/image?download=true').status_code, 200)
            self.assertEqual(bob.get(active_batch).status_code, 404)
            self.assertEqual(self.client.get(archive, params={'scope': 'team'}).status_code, 401)
            self.assertEqual(self.client.get(ref + '/image').status_code, 404)
            template = alice.post('/api/templates', files={'template': ('shared.png', picture('red'), 'image/png')}).json()
            self.assertIn(template['id'], [t['id'] for t in bob.get('/api/templates').json()['templates']])
            self.assertEqual(bob.get(template['url']).content, picture('red'))

            # Before team-wide numbering, two browsers could both download batch (1).
            legacy_meta = next(key for key in self.archive_objects if key.endswith(legacy['items'][0]['id'] + '.json'))
            old = json.loads(self.archive_objects[legacy_meta])
            old['download_folder'] = group['folder']
            self.archive_objects[legacy_meta] = json.dumps(old).encode()
            duplicates = bob.get(archive, params={'view': 'batch_folders', 'refresh': True}).json()['folders']
            self.assertEqual([row['folder'] for row in duplicates], [group['folder']] * 2)
            for row in duplicates:
                contents = bob.get(archive, params={'view': 'page', 'folder': row['folder'], 'batch': row['batch']}).json()
                self.assertEqual([item['id'][:12] for item in contents['items']], [row['batch']])
            bob.post('/api/auth/logout', json={})
            self.assertEqual(bob.delete(ref).status_code, 404)

    def test_archive_tracks_selected_downloads_and_recovers_deleted_results(self):
        files = [('templates', ('one.png', picture('red'), 'image/png')),
                 ('products', ('first.png', picture('green'), 'image/png')),
                 ('products', ('second.png', picture('blue'), 'image/png'))]
        batch = self.client.post('/api/compose-batch', files=files, data={'mode': 'overlay'}).json()
        status = self.client.get(batch['status_url']).json()
        first, second = status['images']
        endpoint = '/api/composition-archive'
        path = f"/api/batches/{batch['batch_id']}/images"
        preview = self.client.get(second['url']).content
        self.assertEqual(self.client.get(endpoint).json()['items'], [])
        prepared = self.client.post(status['download_url'], params={'ids': first['id']})
        self.assertEqual(prepared.status_code, 200)
        download = prepared.json()
        self.assertEqual([item['id'] for item in download['items']], [f"{batch['batch_id']}-{first['id']}"])
        self.assertEqual(TestClient(server.app).get(download['url']).status_code, 404)
        folder = download['filename'].removesuffix('.zip')
        with zipfile.ZipFile(io.BytesIO(self.client.get(download['url']).content)) as zipped:
            self.assertEqual(zipped.namelist(), [f"{folder}/{first['name']}"])
        self.assertEqual(len(self.client.get(endpoint).json()['items']), 1)
        individual = self.client.get(f"{path}/{first['id']}/download")
        self.assertEqual(individual.status_code, 200)
        self.assertEqual(json.loads(unquote(individual.headers["X-Composition-Record"])), download["items"][0])
        self.assertEqual(len(self.client.get(endpoint).json()['items']), 1)

        self.oss_failure = True
        self.assertEqual(self.client.delete(f"{path}/{second['id']}").status_code, 503)
        self.assertEqual(self.client.get(second['url']).status_code, 200)
        self.oss_failure = False
        deletion = self.client.delete(f"{path}/{second['id']}")
        self.assertEqual(deletion.status_code, 200)
        self.assertEqual(deletion.json()['item']['section'], 'trash')
        self.assertEqual(self.client.get(second['url']).status_code, 404)
        self.assertEqual(self.client.get(batch['status_url']).json()['images'], [first])
        entry = next(item for item in self.client.get(endpoint).json()['items'] if item['section'] == 'trash')
        archived_image = f"{endpoint}/{entry['id']}/image"
        other = TestClient(server.app)
        self.assertEqual(other.get(endpoint).json()['items'], [])
        self.assertEqual(other.get(archived_image).status_code, 404)
        self.assertEqual(other.get(batch['status_url']).status_code, 404)
        other.close()
        restoration = self.client.post(f"{endpoint}/{entry['id']}/restore")
        self.assertEqual(restoration.status_code, 200)
        self.assertEqual(restoration.json()['item']['section'], 'history')
        self.assertEqual(self.client.get(archived_image, params={'download': True}).content, preview)
        restored = next(item for item in self.client.get(endpoint).json()['items'] if item['id'] == entry['id'])
        self.assertEqual((restored['section'], restored['expires_at']), ('history', entry['expires_at']))
        batch_dir = Path(self.directory.name) / batch['batch_id']
        disk_status = server._read_batch_status(batch_dir)
        disk_status['finished_at'] = '2020-01-01T00:00:00+00:00'
        server._write_batch_status(batch_dir, disk_status)
        server._purge_composition_storage()
        self.assertFalse(batch_dir.exists())
        self.assertEqual(self.client.get(archived_image, params={'download': True}).content, preview)
        metadata_key = next(key for key in self.archive_objects if key.endswith(entry['id'] + '.json'))
        self.archive_objects[metadata_key] = json.dumps({**restored, 'expires_at': '2020-01-01T00:00:00+00:00'}).encode()
        self.assertEqual(self.client.get(archived_image).status_code, 404)
        server._purge_composition_storage()
        self.assertFalse(any(entry['id'] in key for key in self.archive_objects))


class TemplateLibraryErrorsTest(unittest.TestCase):
    def test_incomplete_template_list_returns_error(self):
        class Failure(Exception):
            pass
        class BrokenBucket:
            def get_object(self, key):
                raise Failure('network error')
        sdk = SimpleNamespace(exceptions=SimpleNamespace(OssError=Failure, NoSuchKey=oss_rest.NoSuchKey), ObjectIteratorV2=lambda bucket, prefix: [SimpleNamespace(key=prefix + '0000000000000001.json')])
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
