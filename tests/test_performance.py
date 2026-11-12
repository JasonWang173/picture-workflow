import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import patch

import numpy as np
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


class TransparentFrameTest(unittest.TestCase):
    def test_cutout_fills_transparent_opening_under_antialiased_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = Image.new("RGBA", (240, 320), (224, 148, 52, 255))
            alpha = frame.getchannel("A")
            draw = ImageDraw.Draw(alpha)
            draw.rounded_rectangle((42, 93, 195, 276), radius=12, fill=92)
            draw.rounded_rectangle((44, 95, 193, 274), radius=10, fill=0)
            frame.putalpha(alpha)
            frame.save(root / "frame.png")
            color = (40, 90, 155)
            Image.new("RGB", (96, 96), color).save(root / "photo.png")

            for size in (frame.size, (120, 160)):
                with self.subTest(size=size):
                    core.compose(root / "photo.png", root / "frame.png", root / "result.png",
                                 mode="cutout", size=size)
                    scaled_frame = frame.resize(size, Image.Resampling.LANCZOS)
                    expected = Image.alpha_composite(Image.new("RGBA", size, color), scaled_frame).convert("RGB")
                    with Image.open(root / "result.png") as result:
                        # The entire frame should equal the original PNG drawn
                        # over the photo, including partial coverage at corners.
                        self.assertEqual(result.tobytes(), expected.tobytes())


class DarkFrameTest(unittest.TestCase):
    def test_cutout_replaces_black_matte_through_rounded_antialiased_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            size = (240, 320)
            frame = Image.new("RGBA", size, (236, 152, 52, 255))
            large_alpha = Image.new("L", (size[0] * 4, size[1] * 4), 255)
            ImageDraw.Draw(large_alpha).rounded_rectangle(
                (42 * 4, 93 * 4, 195 * 4 - 1, 276 * 4 - 1), radius=17 * 4, fill=0,
            )
            frame.putalpha(large_alpha.resize(size, Image.Resampling.BOX))
            ImageDraw.Draw(frame).rectangle((20, 20, 57, 58), fill=(5, 5, 5, 255))
            matte = Image.alpha_composite(Image.new("RGBA", size, "black"), frame).convert("RGB")
            matte.save(root / "frame.png")
            color = (40, 90, 155)
            Image.new("RGB", (96, 96), color).save(root / "photo.png")

            for mode, output_size in (("cutout", size), ("auto", (120, 160))):
                with self.subTest(mode=mode, size=output_size):
                    core.compose(root / "photo.png", root / "frame.png", root / "result.png",
                                 mode=mode, size=output_size)
                    scaled = frame.resize(output_size, Image.Resampling.LANCZOS)
                    expected = Image.alpha_composite(Image.new("RGBA", output_size, color), scaled).convert("RGB")
                    with Image.open(root / "result.png") as result:
                        error = np.abs(np.asarray(result, dtype=np.int16) - np.asarray(expected))
                        self.assertLessEqual(int(error.max()), 5)


class GreenSlotDetectionTest(unittest.TestCase):
    def test_cutout_recovers_jpeg_slot_edges_and_keeps_nearby_green_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = Image.new("RGB", (420, 480), (242, 220, 184))
            draw = ImageDraw.Draw(template)
            draw.rectangle((51, 136, 357, 427), fill=(0, 255, 0))
            draw.rectangle((35, 280, 42, 342), fill=(0, 255, 0))
            draw.polygon(((358, 282), (389, 280), (399, 340), (359, 342)), fill=(65, 158, 82))
            template.save(root / "template.jpg", quality=72, subsampling=2)
            food = (185, 54, 39)
            Image.new("RGB", (96, 96), food).save(root / "product.png")

            core.compose(root / "product.png", root / "template.jpg", root / "out.png", mode="cutout")
            with Image.open(root / "template.jpg") as source, Image.open(root / "out.png") as result:
                for xy in ((51, 220), (135, 136), (357, 220), (135, 427)):
                    self.assertEqual(result.getpixel(xy), food, xy)
                for xy in ((38, 315), (372, 315)):
                    self.assertEqual(result.getpixel(xy), source.getpixel(xy), xy)
                edge = np.asarray(result.crop((44, 130, 363, 435)), dtype=np.int16)
                green = (edge[..., 1] >= 100) & (edge[..., 1] - np.maximum(edge[..., 0], edge[..., 2]) >= 25)
                green[150 - 130:350 - 130, 358 - 44:] = False  # Adjacent green decoration.
                self.assertLessEqual(np.count_nonzero(green), 10)

    def test_neon_panel_isolated_from_connected_green_artwork(self):
        template = Image.new('RGB', (640, 360), (170, 210, 245))
        draw = ImageDraw.Draw(template)
        draw.rectangle((0, 285, 639, 359), fill=(65, 158, 82))
        draw.rounded_rectangle((345, 45, 614, 312), radius=28, fill=(0, 254, 0))
        # Bright narrow leaves connect the panel to a broad natural-green area.
        draw.polygon(((550, 312), (560, 215), (570, 359), (548, 359)), fill=(0, 254, 0))
        draw.polygon(((120, 359), (135, 250), (145, 359)), fill=(0, 245, 20))
        detected = core._template_replace_slot(template)
        self.assertIsNotNone(detected)
        (x0, y0, x1, y1), mask = detected
        self.assertLessEqual(abs(x0 - 345), 2)
        self.assertLessEqual(abs(y0 - 45), 2)
        self.assertLessEqual(abs(x1 - 615), 2)
        self.assertLessEqual(abs(y1 - 313), 2)
        active = np.asarray(mask.convert('L')) >= 128
        self.assertFalse(active[330, 560])
        self.assertFalse(active[320, 120])
        self.assertTrue(active[180, 480])

    def test_cutout_preserves_rounded_corners_over_grass_and_removes_key_spill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sky, grass, green, food = (170, 210, 245), (65, 158, 82), (0, 254, 0), (188, 48, 30)
            template = Image.new('RGB', (640, 360), sky)
            draw = ImageDraw.Draw(template)
            draw.rectangle((0, 270, 639, 359), fill=grass)
            draw.rounded_rectangle((345, 45, 614, 312), radius=34, fill=green)
            # Simulate a decoded JPEG pixel contaminated by the key color.
            draw.line((344, 100, 344, 200), fill=(136, 219, 196))
            template.save(root / 'template.png')
            Image.new('RGB', (80, 60), food).save(root / 'product.png')

            for mode, size, offset in (('cutout', '640x360', 0), ('auto', '640x640', 140)):
                with self.subTest(mode=mode):
                    core.compose(root / 'product.png', root / 'template.png', root / 'out.png',
                                 mode=mode, size=size)
                    with Image.open(root / 'out.png') as result:
                        self.assertEqual(result.getpixel((611, 309 + offset)), grass)
                        self.assertEqual(result.getpixel((348, 309 + offset)), grass)
                        self.assertEqual(result.getpixel((480, 311 + offset)), food)
                        self.assertEqual(result.getpixel((344, 140 + offset)), sky)
                        self.assertEqual(result.getpixel((345, 140 + offset)), food)
                        self.assertEqual(result.getpixel((200, 310 + offset)), grass)

    def test_dark_jpeg_panel_does_not_capture_nearby_shades_of_green(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grass, food = (65, 158, 82), (188, 48, 30)
            template = Image.new('RGB', (640, 360), (170, 210, 245))
            draw = ImageDraw.Draw(template)
            draw.rectangle((0, 255, 639, 359), fill=grass)
            draw.ellipse((18, 30, 110, 86), fill=(0, 245, 15))
            draw.rounded_rectangle((345, 45, 614, 312), radius=36, fill=(40, 128, 56))
            template.save(root / 'template.jpg', quality=82)
            Image.new('RGB', (80, 60), food).save(root / 'product.png')
            core.compose(root / 'product.png', root / 'template.jpg', root / 'out.png', mode='cutout')
            with Image.open(root / 'template.jpg') as source, Image.open(root / 'out.png') as result:
                for xy in ((611, 309), (348, 309), (65, 55), (200, 315)):
                    self.assertEqual(result.getpixel(xy), source.getpixel(xy), xy)
                self.assertEqual(result.getpixel((480, 290)), food)

    def test_green_artwork_without_panel_does_not_become_a_product_slot(self):
        template = Image.new('RGB', (640, 360), (170, 210, 245))
        draw = ImageDraw.Draw(template)
        draw.rectangle((0, 252, 639, 359), fill=(60, 158, 80))
        draw.ellipse((18, 30, 110, 86), fill=(0, 245, 15))
        self.assertIsNone(core._template_replace_slot(template))

    def test_similar_green_panels_require_a_choice_and_manual_cutout_respects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = Image.new('RGB', (400, 200), (170, 210, 245))
            draw = ImageDraw.Draw(template)
            draw.rectangle((20, 40, 179, 159), fill=(0, 254, 0))
            draw.rectangle((220, 40, 379, 159), fill=(0, 254, 0))
            template.save(root / 'template.png')
            Image.new('RGB', (80, 60), (188, 48, 30)).save(root / 'product.png')
            for mode in ('auto', 'cutout', 'template_replace'):
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, '多个相似绿幕'):
                    core.compose(root / 'product.png', root / 'template.png', root / 'out.png', mode=mode)
            core.compose(root / 'product.png', root / 'template.png', root / 'out.png',
                         mode='cutout', manual_slot=(.55, .2, .4, .6))
            expected = template.copy()
            expected.paste((188, 48, 30), (220, 40, 380, 160))
            with Image.open(root / 'out.png') as result:
                self.assertEqual(result.tobytes(), expected.tobytes())


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
