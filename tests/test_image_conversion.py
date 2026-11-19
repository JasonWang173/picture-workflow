import io
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import server
from food_compositor import image_conversion as core
from food_compositor.cancellation import check_cancelled
from food_compositor.conversion_api import ConversionJobs
from food_compositor.request_limits import RequestLimits
from food_compositor.storage_guard import StorageFull


def encoded(image, format='PNG', **options):
    output = io.BytesIO()
    image.save(output, format, **options)
    return output.getvalue()


class ConversionCoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_png_preserves_jpeg_dimensions_pixels_and_exif_without_rotation(self):
        pixels = np.zeros((30, 90, 3), dtype=np.uint8)
        pixels[:15, :45] = (220, 20, 30)
        pixels[15:, 45:] = (20, 40, 220)
        source = Image.fromarray(pixels)
        exif = Image.Exif(); exif[274] = 6
        source.save(self.root/'source.jpg', quality=98, exif=exif)
        metadata = core.convert_image(self.root/'source.jpg', self.root/'result.png', 'png')
        with Image.open(self.root/'source.jpg') as original, Image.open(self.root/'result.png') as result:
            self.assertEqual(result.size, (90, 30))
            self.assertEqual(result.getexif()[274], 6)
            np.testing.assert_array_equal(np.asarray(result), np.asarray(original))
        self.assertEqual((metadata['width'], metadata['height']), (90, 30))

    def test_same_static_format_is_copied_without_reencoding(self):
        for format, suffix, mode in [('JPEG', '.jpg', 'RGB'), ('PNG', '.png', 'RGBA')]:
            source = self.root/('source'+suffix)
            Image.new(mode, (43, 27), 'red').save(source)
            target = self.root/('copy'+suffix)
            core.convert_image(source, target, format.lower())
            self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_transparency_png_retained_and_jpeg_flattened_to_white(self):
        image = Image.new('RGBA', (30, 20), (250, 10, 20, 0))
        source = self.root/'source.webp'
        image.save(source, lossless=True)
        core.convert_image(source, self.root/'alpha.png', 'png')
        core.convert_image(source, self.root/'white.jpg', 'jpeg')
        with Image.open(self.root/'alpha.png') as result:
            self.assertEqual(result.getchannel('A').getextrema(), (0, 0))
        with Image.open(self.root/'white.jpg') as result:
            self.assertGreaterEqual(min(result.getpixel((5, 5))), 253)

    def test_selected_gif_frame_not_always_first_frame(self):
        frames = [Image.new('RGB', (35, 23), color) for color in ('red', 'green', 'blue')]
        source = self.root/'animation.gif'
        frames[0].save(source, save_all=True, append_images=frames[1:], duration=60, loop=0, disposal=2)
        self.assertEqual(core.inspect_image(source)['frames'], 3)
        with self.assertRaisesRegex(ValueError, '选择'):
            core.convert_image(source, self.root/'missing.png', 'png')
        result = core.convert_image(source, self.root/'third.png', 'png', frame=2)
        with Image.open(self.root/'third.png') as image:
            self.assertEqual(image.size, (35, 23))
            self.assertEqual(image.convert('RGB').getpixel((5, 5)), (0, 0, 255))
            self.assertEqual(getattr(image, 'n_frames', 1), 1)
        self.assertEqual(result['frame'], 2)
        self.assertIn('第 3', result['warnings'][0])

    def test_selected_tiff_page_keeps_its_own_dimensions(self):
        source = self.root/'pages.tiff'
        Image.new('RGB', (30, 20), 'red').save(source, save_all=True, append_images=[Image.new('RGB', (45, 70), 'blue')])
        core.convert_image(source, self.root/'second.png', 'png', frame=1)
        with Image.open(self.root/'second.png') as result:
            self.assertEqual(result.size, (45, 70))
            self.assertEqual(result.getpixel((3, 3)), (0, 0, 255))

    def test_true_content_detection_and_multiple_raster_formats(self):
        for format in ('BMP', 'TIFF', 'WEBP', 'ICO', 'AVIF', 'JPEG2000'):
            with self.subTest(format=format):
                source = self.root/'misleading.bin'
                source.write_bytes(encoded(Image.new('RGB', (32, 32), 'red'), format))
                core.convert_image(source, self.root/'result.png', 'png')
                with Image.open(self.root/'result.png') as result:
                    self.assertEqual(result.size, (32, 32))

    def test_heic_when_plugin_is_available(self):
        if not core.capabilities()['heif']:
            self.skipTest('HEIF plugin not installed')
        source = self.root/'phone.heic'
        Image.new('RGB', (45, 27), 'blue').save(source, 'HEIF', quality=95)
        core.convert_image(source, self.root/'phone.png', 'png')
        with Image.open(self.root/'phone.png') as result:
            self.assertEqual(result.size, (45, 27))

    def test_no_poster_size_or_300kb_constraint(self):
        image = Image.fromarray(np.random.default_rng(5).integers(0, 256, (512, 512, 3), dtype=np.uint8))
        image.save(self.root/'source.bmp')
        for output in ('png', 'jpeg'):
            path = self.root/('output.'+output)
            metadata = core.convert_image(self.root/'source.bmp', path, output)
            self.assertEqual((metadata['width'], metadata['height']), (512, 512))
            self.assertGreater(path.stat().st_size, 300_000)

    def test_16bit_grayscale_and_invalid_inputs(self):
        Image.fromarray(np.array([[0, 65535]], dtype=np.uint16)).save(self.root/'gray.tiff')
        core.convert_image(self.root/'gray.tiff', self.root/'gray.png', 'png')
        with Image.open(self.root/'gray.png') as result:
            self.assertEqual(result.getextrema(), (0, 65535))
        core.convert_image(self.root/'gray.tiff', self.root/'gray.jpg', 'jpeg')
        with Image.open(self.root/'gray.jpg') as result:
            self.assertEqual(result.mode, 'L')
            result = result.convert('RGB')
            self.assertLess(max(result.getpixel((0, 0))), 4)
            self.assertGreater(min(result.getpixel((1, 0))), 250)
        with patch.object(core, 'MAX_PIXELS', 1), self.assertRaisesRegex(ValueError, '像素'):
            core.convert_image(self.root/'gray.tiff', self.root/'oversized.png', 'png')
        self.assertFalse((self.root/'oversized.png').exists())
        with self.assertRaisesRegex(ValueError, '输出格式'):
            core.convert_image(self.root/'gray.tiff', self.root/'output.bmp', 'bmp')


class ConversionAPITest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.jobs = ConversionJobs(self.root, server._workspace_owner, server._write_upload)
        app = FastAPI()
        app.include_router(self.jobs.router)
        app.add_middleware(RequestLimits, storage=self.root)
        self.client = TestClient(app)
        self.other = TestClient(app)
        self.executor = ThreadPoolExecutor(max_workers=2)
        def submit(function, *args, estimated_bytes=None, cancel_check=None, **kwargs):
            if cancel_check:
                cancel_check()
            return self.executor.submit(function, *args, **kwargs)
        self.patches = [patch('food_compositor.conversion_api.submit_composition', side_effect=submit),
                        patch('food_compositor.conversion_api.worker_count', return_value=2)]
        for value in self.patches: value.start()

    def tearDown(self):
        self.executor.shutdown(wait=True)
        for value in reversed(self.patches): value.stop()
        self.client.close(); self.other.close(); self.directory.cleanup()

    def upload(self, files, format='png'):
        job = self.client.post('/api/converter/reserve').json()['id']
        response = self.client.post('/api/converter/upload', data={'job': job, 'output_format': format},
                                    files=[('files', (name, content, 'application/octet-stream')) for name, content in files])
        self.assertEqual(response.status_code, 200, response.text)
        return job, self.client.get('/api/converter/'+job).json()

    def test_independent_png_batch_bad_file_unique_names_mime_ownership_and_zip(self):
        with patch.object(server, '_oss_bucket', side_effect=AssertionError('converter must not access OSS')):
            job, state = self.upload([('重复.jpg', encoded(Image.new('RGB', (73, 41), 'red'), 'BMP')),
                                      ('重复.jpg', encoded(Image.new('RGBA', (29, 63), (20, 30, 40, 0)))), ('bad.bin', b'invalid image')])
            self.assertEqual(state['status'], 'selecting')
            self.assertEqual(self.other.get('/api/converter/'+job).status_code, 404)
            response = self.client.post('/api/converter/start', json={'id': job, 'frames': {}})
            self.assertEqual(response.status_code, 200, response.text)
            state = self.client.get('/api/converter/'+job).json()
            self.assertEqual((state['status'], state['done'], len(state['images']), len(state['skipped'])), ('done', 3, 2, 1))
            first, second = state['images']
            self.assertNotEqual(first['name'], second['name'])
            preview = self.client.get(second['url'])
            self.assertEqual(preview.headers['content-type'], 'image/png')
            with Image.open(io.BytesIO(preview.content)) as image:
                self.assertEqual(image.size, (29, 63))
                self.assertEqual(image.getchannel('A').getextrema(), (0, 0))
            self.assertEqual(self.other.get(second['download_url']).status_code, 404)
            self.assertEqual(self.client.get(first['download_url']).status_code, 200)
            download = self.client.post(f'/api/converter/{job}/download').json()['url']
            self.assertEqual(self.other.get(download).status_code, 404)
            with zipfile.ZipFile(io.BytesIO(self.client.get(download).content)) as archive:
                self.assertEqual(archive.namelist(), [first['name'], second['name']])
                self.assertTrue(all(name.endswith('.png') for name in archive.namelist()))
            self.assertFalse((self.root/job/'inputs').exists())

    def test_selected_frame_is_required_validated_previewed_and_exported(self):
        frames = [Image.new('RGB', (40, 35), color) for color in ('red', 'blue')]
        content = encoded(frames[0], 'GIF', save_all=True, append_images=frames[1:], duration=80)
        job, state = self.upload([('animation.gif', content)])
        self.assertEqual(state['inputs'][0]['frames'], 2)
        self.assertEqual(self.client.post('/api/converter/start', json={'id': job, 'frames': {}}).status_code, 400)
        self.assertEqual(self.client.post('/api/converter/start', json={'id': job, 'frames': {'1': 2}}).status_code, 400)
        self.assertEqual(self.other.get(f'/api/converter/{job}/frame/1/1').status_code, 404)
        self.assertEqual(self.client.get(f'/api/converter/{job}/frame/1/2').status_code, 400)
        preview = self.client.get(f'/api/converter/{job}/frame/1/1')
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(preview.content)).convert('RGB').getpixel((2, 2)), (0, 0, 255))
        self.client.post('/api/converter/start', json={'id': job, 'frames': {'1': 1}})
        output = self.client.get('/api/converter/'+job).json()['images'][0]
        self.assertEqual(output['frame'], 1)
        self.assertEqual(Image.open(io.BytesIO(self.client.get(output['url']).content)).convert('RGB').getpixel((2, 2)), (0, 0, 255))

    def test_cancel_reserved_and_selecting_jobs_and_reject_duplicate_upload(self):
        job = self.client.post('/api/converter/reserve').json()['id']
        self.assertEqual(self.client.post(f'/api/converter/{job}/cancel').json()['status'], 'cancelled')
        self.assertEqual(self.client.post('/api/converter/upload', data={'job': job}, files={'files': ('a.png', encoded(Image.new('RGB', (5, 5))))}).status_code, 409)
        job, _ = self.upload([('a.png', encoded(Image.new('RGB', (5, 5))))])
        self.assertEqual(self.client.post(f'/api/converter/{job}/cancel').json()['status'], 'cancelled')
        self.assertFalse((self.root/job/'inputs').exists())
        self.assertEqual(self.client.post('/api/converter/start', json={'id': job, 'frames': {}}).status_code, 409)

    def test_cancellation_keeps_completed_results_and_next_job_works(self):
        job, _ = self.upload([(f'{i}.png', encoded(Image.new('RGB', (40, 30), 'red'))) for i in range(3)])
        started = threading.Event()
        original = core.convert_image
        calls = []
        def convert(source, destination, *args, **kwargs):
            calls.append(Path(source).name)
            if len(calls) == 2:
                started.set()
                deadline = time.monotonic()+3
                while time.monotonic()<deadline:
                    check_cancelled(); time.sleep(.01)
            return original(source, destination, *args, **kwargs)
        with patch('food_compositor.conversion_api.worker_count', return_value=1), patch('food_compositor.conversion_api.convert_image', side_effect=convert), ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(self.client.post, '/api/converter/start', json={'id': job, 'frames': {}})
            self.assertTrue(started.wait(3))
            owner = TestClient(self.client.app); owner.cookies.update(self.client.cookies)
            self.assertEqual(owner.post(f'/api/converter/{job}/cancel').status_code, 200)
            result.result(timeout=4)
            owner.close()
        state = self.client.get('/api/converter/'+job).json()
        self.assertEqual((state['status'], state['done'], len(state['images'])), ('cancelled', 1, 1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.client.get(state['images'][0]['url']).status_code, 200)
        next_job, _ = self.upload([('next.png', encoded(Image.new('RGB', (10, 20))))])
        self.client.post('/api/converter/start', json={'id': next_job, 'frames': {}})
        self.assertEqual(self.client.get('/api/converter/'+next_job).json()['status'], 'done')

    def test_zip_disk_guard_keeps_results_and_cleanup_expiration(self):
        job, _ = self.upload([('a.png', encoded(Image.new('RGB', (40, 30))))])
        self.client.post('/api/converter/start', json={'id': job, 'frames': {}})
        state = self.client.get('/api/converter/'+job).json()
        with patch('food_compositor.conversion_api.ensure_space', side_effect=StorageFull('磁盘不足')):
            self.assertEqual(self.client.post(f'/api/converter/{job}/download').status_code, 507)
        self.assertEqual(list((self.root/job).glob('*.zip')), [])
        self.assertEqual(self.client.get(state['images'][0]['url']).status_code, 200)
        old = self.jobs.read(job); old['finished_at'] = '2020-01-01T00:00:00+00:00'; self.jobs.write(job, old)
        self.jobs.purge()
        self.assertEqual(self.client.get('/api/converter/'+job).status_code, 404)

    def test_frame_preview_does_not_lock_out_cancel_or_delete_inputs_too_early(self):
        frames = [Image.new('RGB', (40, 35), color) for color in ('red', 'blue')]
        job, _ = self.upload([('a.gif', encoded(frames[0], 'GIF', save_all=True, append_images=frames[1:]))])
        started = threading.Event()
        def preview(*args):
            started.set()
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                check_cancelled(); time.sleep(.01)
            return b'not used'
        with patch('food_compositor.conversion_api.preview_frame', side_effect=preview), ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(self.client.get, f'/api/converter/{job}/frame/1/0')
            self.assertTrue(started.wait(3))
            owner = TestClient(self.client.app); owner.cookies.update(self.client.cookies)
            before = time.monotonic()
            self.assertEqual(owner.post(f'/api/converter/{job}/cancel').json()['status'], 'cancelled')
            self.assertLess(time.monotonic()-before, 1)
            self.assertEqual(result.result(timeout=2).status_code, 400)
            owner.close()
        self.assertFalse((self.root/job/'inputs').exists())
        self.assertEqual(list((self.root/job).glob('preview-*.lease')), [])
