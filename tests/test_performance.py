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
