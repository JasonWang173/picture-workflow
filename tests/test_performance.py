import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from food_compositor import core, execution


class TemplateReuseTest(unittest.TestCase):
    def test_cache_preserves_outputs_and_invalidates_replaced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / 'template.png'
            Image.new('RGB', (80, 100), 'blue').save(root/'a.png')
            Image.new('RGB', (100, 80), 'red').save(root/'b.png')
            def save_template(color):
                image = Image.new('RGB', (80, 100), color)
                ImageDraw.Draw(image).rectangle((10, 30, 70, 90), fill=(0,255,0))
                image.save(template)
            save_template('yellow')
            for mode in ('cutout', 'overlay', 'auto', 'template_replace'):
                core._prepare_template.cache_clear()
                for product in ('a', 'b', 'a'):
                    core.compose(None if mode == 'template_replace' else root/(product+'.png'), template, root/'cached.jpg', mode=mode, size='80x100')
                    cached = (root/'cached.jpg').read_bytes()
                    core._prepare_template.cache_clear()
                    core.compose(None if mode == 'template_replace' else root/(product+'.png'), template, root/'fresh.jpg', mode=mode, size='80x100')
                    self.assertEqual(cached, (root/'fresh.jpg').read_bytes())
            core.compose(root/'a.png', template, root/'before.jpg', mode='cutout', size='80x100')
            save_template('purple')
            core.compose(root/'a.png', template, root/'changed.jpg', mode='cutout', size='80x100')
            self.assertNotEqual((root/'before.jpg').read_bytes(), (root/'changed.jpg').read_bytes())
            core._prepare_template.cache_clear()
            core.compose(root/'a.png', template, root/'fresh.jpg', mode='cutout', size='80x100')
            self.assertEqual((root/'changed.jpg').read_bytes(), (root/'fresh.jpg').read_bytes())

    def test_manual_regions_do_not_share_prepared_masks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (100,100), 'blue').save(root/'template.png')
            core._prepare_template.cache_clear()
            results = []
            for rect in ((.1,.2,.3,.5), (.6,.2,.3,.5)):
                core.compose(None, root/'template.png', root/'out.jpg', mode='template_replace', size='100x100', manual_slot=rect)
                results.append((root/'out.jpg').read_bytes())
            self.assertNotEqual(results[0], results[1])
            self.assertEqual(core._prepare_template.cache_info().misses, 2)

    def test_manual_region_is_an_exact_hard_boundary_without_auto_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_path = root / 'template.png'
            Image.new('RGB', (100, 100), (23, 45, 67)).save(template_path)
            core._prepare_template.cache_clear()
            with patch.object(core, '_template_replace_slot', side_effect=AssertionError('manual selection must not run auto detection')):
                _, _, page, detected, selected, _ = core._prepare_template(
                    core._template_identity(template_path), '100x100', 'template_replace', True, .78, (.2, .3, .4, .25))
            self.assertEqual(selected, 'template_replace')
            self.assertIsNotNone(detected)
            self.assertEqual(detected[0], (20, 30, 60, 55))
            blank = core._remove_template_product(page, detected=detected)
            self.assertEqual(blank.getpixel((19, 40)), (23, 45, 67))
            self.assertEqual(blank.getpixel((20, 30)), (0, 255, 0))
            self.assertEqual(blank.getpixel((59, 54)), (0, 255, 0))
            self.assertEqual(blank.getpixel((60, 54)), (23, 45, 67))

            core._prepare_template.cache_clear()
            with patch.object(core, '_template_replace_slot', side_effect=AssertionError('manual selection must not run auto detection')):
                _, _, wide_page, wide_detected, _, _ = core._prepare_template(
                    core._template_identity(template_path), '200x100', 'template_replace', True, .78, (.2, .3, .4, .25))
            self.assertEqual(wide_page.size, (200, 100))
            self.assertEqual(wide_detected[0], (70, 30, 110, 55))
            wide_blank = core._remove_template_product(wide_page, detected=wide_detected)
            self.assertEqual(wide_blank.getpixel((69, 40)), (23, 45, 67))
            self.assertEqual(wide_blank.getpixel((70, 40)), (0, 255, 0))
            self.assertEqual(wide_blank.getpixel((109, 54)), (0, 255, 0))
            self.assertEqual(wide_blank.getpixel((110, 54)), (23, 45, 67))

            output_path = root / 'manual-scaled.png'
            core.compose(None, template_path, output_path, mode='template_replace', size='200x100', manual_slot=(.2, .3, .4, .25))
            with Image.open(output_path) as output:
                self.assertEqual(output.getpixel((69, 40)), (23, 45, 67))
                self.assertEqual(output.getpixel((70, 40)), (0, 255, 0))
                self.assertEqual(output.getpixel((109, 54)), (0, 255, 0))
                self.assertEqual(output.getpixel((110, 54)), (23, 45, 67))

    def test_settings_are_part_of_cache_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (100,100), 'green').save(root/'template.png')
            Image.new('RGB', (100,100), 'red').save(root/'product.png')
            core._prepare_template.cache_clear()
            for size in ('80x100', '100x80'):
                core.compose(root/'product.png', root/'template.png', root/'out.jpg', mode='cutout', size=size)
                with Image.open(root/'out.jpg') as image:
                    self.assertEqual(image.size, tuple(map(int,size.split('x'))))
            self.assertEqual(core._prepare_template.cache_info().maxsize, 1)


class SharedExecutionTest(unittest.TestCase):
    def tearDown(self):
        execution.shutdown_executor()

    def test_global_worker_and_memory_limits_across_requests(self):
        for reservation, expected in ((256, 2), (800, 1)):
            execution.shutdown_executor()
            active = 0
            maximum = 0
            lock = threading.Lock()
            def job():
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(.02)
                with lock:
                    active -= 1
                return 'ok'
            def create_pool(**settings):
                return ThreadPoolExecutor(max_workers=settings['max_workers'])
            with patch.object(execution, 'ProcessPoolExecutor', side_effect=create_pool), patch.object(execution, 'worker_count', return_value=2), patch.object(execution, 'memory_budget', return_value=1024):
                with ThreadPoolExecutor(max_workers=6) as callers:
                    results = list(callers.map(lambda _: execution.submit_composition(job, estimated_bytes=reservation).result(), range(6)))
                self.assertEqual(results, ['ok'] * 6)
                self.assertEqual(maximum, expected)
                self.assertEqual(execution._active, 0)
                self.assertEqual(execution._reserved, 0)

    def test_broken_pool_releases_admission_and_can_be_recreated(self):
        class Broken:
            def submit(self, *args, **kwargs):
                raise BrokenProcessPool('worker terminated')
            def shutdown(self, **kwargs):
                pass
        good = ThreadPoolExecutor(max_workers=1)
        try:
            with patch.object(execution, 'ProcessPoolExecutor', side_effect=[Broken(), good]), patch.object(execution, 'worker_count', return_value=1), patch.object(execution, 'memory_budget', return_value=1024):
                with self.assertRaises(BrokenProcessPool):
                    execution.submit_composition(str, 'first', estimated_bytes=512)
                self.assertEqual(execution._active, 0)
                self.assertEqual(execution.submit_composition(str, 'second', estimated_bytes=512).result(), 'second')
        finally:
            execution.shutdown_executor()

    def test_invalid_worker_override_uses_safe_hardware_limit(self):
        with patch.object(execution, 'cpu_limit', return_value=2), patch.object(execution, 'memory_budget', return_value=2048*execution.MIB):
            with patch.dict('os.environ', {'BATCH_WORKERS': '99'}):
                self.assertEqual(execution.worker_count(), 2)
            with patch.dict('os.environ', {'BATCH_WORKERS': 'bad'}):
                self.assertEqual(execution.worker_count(), 2)


if __name__ == '__main__':
    unittest.main()
