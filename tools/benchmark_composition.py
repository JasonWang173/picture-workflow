"""Repeatable speed/quality check using deterministic fixtures; no network writes."""
import argparse
import cProfile
import hashlib
import importlib.util
import json
import pstats
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def load(path):
    spec = importlib.util.spec_from_file_location('bench_core', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    width, height = 405, 720
    slot = Image.new('RGB', (width, height), (220, 200, 180))
    draw = ImageDraw.Draw(slot)
    draw.rectangle((30, 180, 375, 620), fill=(0, 255, 0))
    draw.text((25, 40), 'PRODUCT SALE 50%', fill='black')
    slot.save(root / 'slot.png')
    alpha = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(alpha)
    draw.rectangle((10, 15, 390, 90), fill=(240, 130, 80, 245))
    draw.text((25, 35), 'PRODUCT SALE', fill='white')
    alpha.save(root / 'alpha.png')
    flat = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(flat)
    draw.text((25, 40), 'PRODUCT SALE', fill=(220, 100, 20))
    draw.rectangle((20, 650, 380, 700), fill='black')
    flat.save(root / 'flat.jpg', quality=95)
    rng = np.random.default_rng(42)
    pixels = rng.integers(0, 256, (600, 800, 3), dtype=np.uint8)
    Image.fromarray(pixels).filter(ImageFilter.GaussianBlur(2)).save(root / 'product.jpg', quality=95)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--size', default='1080x1920')
    args = parser.parse_args()
    root = Path(args.root)
    fixtures(root)
    core = load(args.core)
    if core.cv2 is not None:
        core.cv2.setNumThreads(1)
    cases = [('overlay-alpha','overlay','alpha.png'), ('overlay-jpg','overlay','flat.jpg'), ('cutout-slot','cutout','slot.png'), ('auto-alpha','auto','alpha.png'), ('auto-slot','auto','slot.png'), ('template-replace','template_replace','slot.png')]
    results = {}
    profile = cProfile.Profile()
    if args.profile: profile.enable()
    for label, mode, template in cases:
        started = time.perf_counter()
        hashes = []
        for i in range(args.repeats):
            output = root / (label + '.jpg')
            core.compose(None if mode == 'template_replace' else root/'product.jpg', root/template, output, mode=mode, size=args.size)
            hashes.append(hashlib.sha256(output.read_bytes()).hexdigest())
        results[label] = {'seconds': round(time.perf_counter()-started, 3), 'sha256': hashes[-1], 'repeat_identical': len(set(hashes)) == 1, 'bytes': output.stat().st_size}
        print(label, results[label], flush=True)
    if args.profile:
        profile.disable()
        pstats.Stats(profile).sort_stats('cumulative').print_stats(15)
    (root/'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
