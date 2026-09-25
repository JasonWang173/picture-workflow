from __future__ import annotations

import argparse
import logging

from .core import compose_many


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="商品图与商家模板图片合成工具")
    parser.add_argument("--products", nargs="+", required=True, help="一个或多个商品图片路径")
    parser.add_argument("--template", required=True, help="商家背景/活动模板路径")
    parser.add_argument("--output-dir", default="./outputs", help="输出目录，默认 ./outputs")
    parser.add_argument("--mode", choices=("auto", "overlay", "cutout"), default="auto")
    parser.add_argument("--size", default=None, help="输出尺寸，例如 1080x1920；默认沿用模板尺寸")
    parser.add_argument("--logo-pill", action=argparse.BooleanOptionalAction, default=True, help="保留左上角 Logo 白色胶囊")
    parser.add_argument("--outline", type=float, default=0.78, help="模板图形白色描边强度 0..1")
    parser.add_argument("--quality", type=int, default=95, help="JPEG 输出质量 1..100")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    results = compose_many(
        args.products,
        args.template,
        args.output_dir,
        mode=args.mode,
        size=args.size,
        preserve_logo_pill=args.logo_pill,
        outline_strength=max(0.0, min(1.0, args.outline)),
        quality=args.quality,
    )
    for path in results:
        print(path.resolve())


if __name__ == "__main__":
    main()
