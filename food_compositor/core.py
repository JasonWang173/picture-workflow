from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

try:  # OpenCV is only used by the cutout fallback.
    import cv2
except Exception:  # pragma: no cover - optional at import time
    cv2 = None

LOGGER = logging.getLogger(__name__)
Mode = Literal["auto", "overlay", "cutout"]
_REMBG_SESSION = None


def _open_rgb(path: str | Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _parse_size(size: tuple[int, int] | str | None, fallback: tuple[int, int]) -> tuple[int, int]:
    if size is None:
        return fallback
    if isinstance(size, tuple):
        return size
    try:
        width, height = (int(part) for part in size.lower().replace(" ", "").split("x", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("size 应为例如 1080x1920") from exc
    if width < 64 or height < 64:
        raise ValueError("输出尺寸至少为 64x64")
    return width, height


def fit_cover(image: Image.Image, size: tuple[int, int], focal: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Crop an image to fill a canvas without distorting the subject."""
    width, height = size
    # Avoid an unnecessary second resample when a template already matches the
    # requested canvas. This keeps small campaign type and logo edges crisp.
    if image.size == size:
        return image.copy()
    scale = max(width / image.width, height / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    if scale < 0.98:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=0.45, percent=105, threshold=3))
    left = round((resized.width - width) * min(max(focal[0], 0), 1))
    top = round((resized.height - height) * min(max(focal[1], 0), 1))
    return resized.crop((left, top, left + width, top + height))


def _soft_white_alpha(image: Image.Image, threshold: float = 235.0, softness: float = 23.0) -> Image.Image:
    """Convert a light template background to alpha while retaining anti-aliased edges."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    min_channel = rgb.min(axis=2)
    chroma = rgb.max(axis=2) - min_channel
    neutral = chroma < 18
    distance = (threshold - min_channel) / max(softness, 1)
    alpha = np.where(neutral, np.clip(distance, 0, 1), 1.0)
    return Image.fromarray(np.uint8(alpha * 255), "L").filter(ImageFilter.GaussianBlur(0.35))


def _top_left_logo_bbox(template: Image.Image, alpha: Image.Image) -> tuple[int, int, int, int] | None:
    """Find a compact logo in the top-left quarter for a readable white pill."""
    a = np.asarray(alpha, dtype=np.uint8)
    height, width = a.shape
    region = np.zeros_like(a)
    # The supplied templates place the merchant mark in the first ~10% of the
    # canvas. Keeping this region narrow avoids treating the campaign headline
    # below it as part of the logo pill.
    region[: int(height * 0.10), : int(width * 0.62)] = a[: int(height * 0.10), : int(width * 0.62)]
    mask = (region > 150).astype(np.uint8)
    if cv2 is not None:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        boxes = []
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if area >= max(8, width * height * 0.00002):
                boxes.append((x, y, x + w, y + h))
        if not boxes:
            return None
        x0 = min(box[0] for box in boxes)
        y0 = min(box[1] for box in boxes)
        x1 = max(box[2] for box in boxes)
        y1 = max(box[3] for box in boxes)
    else:
        ys, xs = np.where(mask > 0)
        if len(xs) < 8:
            return None
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    # Prevent the title text from being mistaken for a logo.
    if (x1 - x0) > width * 0.58 or (y1 - y0) > height * 0.12:
        return None
    pad_x, pad_y = max(14, width // 40), max(10, height // 90)
    return max(0, x0 - pad_x), max(0, y0 - pad_y), min(width, x1 + pad_x), min(height, y1 + pad_y)


def _apply_overlay(
    background: Image.Image,
    template: Image.Image,
    *,
    preserve_logo_pill: bool = True,
    outline_strength: float = 0.78,
) -> Image.Image:
    size = background.size
    page = fit_cover(template, size)
    alpha = _soft_white_alpha(page)

    if preserve_logo_pill:
        bbox = _top_left_logo_bbox(page, alpha)
        if bbox is not None:
            pill = Image.new("RGBA", size, (255, 255, 255, 0))
            ImageDraw.Draw(pill).rounded_rectangle(
                bbox,
                radius=max(12, round(min(size) * 0.025)),
                fill=(255, 255, 255, 248),
            )
            background = Image.alpha_composite(background.convert("RGBA"), pill)

    graphic = page.convert("RGBA")
    graphic.putalpha(alpha)

    # A restrained white keyline keeps orange promotional type legible over food.
    if outline_strength > 0.01:
        expanded = alpha.filter(ImageFilter.MaxFilter(9))
        border = ImageChops.subtract(expanded, alpha)
        border = border.point(lambda value: round(value * max(0.0, min(1.0, outline_strength))))
        keyline = Image.new("RGBA", size, (255, 255, 255, 0))
        keyline.putalpha(border)
        background = Image.alpha_composite(background.convert("RGBA"), keyline)

    return Image.alpha_composite(background.convert("RGBA"), graphic).convert("RGB")


def _rembg_cutout(image: Image.Image) -> Image.Image | None:
    global _REMBG_SESSION
    try:
        from rembg import new_session, remove
    except Exception:
        return None
    try:
        # rembg 2.0.85 defaults to the 1 GB BRIA model. u2netp is a much
        # smaller model and is a practical default for a reusable CLI tool.
        if _REMBG_SESSION is None:
            _REMBG_SESSION = new_session("u2netp")
        result = remove(image.convert("RGBA"), session=_REMBG_SESSION)
        if isinstance(result, Image.Image):
            return result.convert("RGBA")
        return Image.open(io.BytesIO(result)).convert("RGBA")
    except Exception as exc:  # model download or runtime can fail offline
        LOGGER.warning("rembg 抠图失败，改用 GrabCut: %s", exc)
        return None


def _grabcut_cutout(image: Image.Image) -> Image.Image:
    """A dependency-light fallback for centered product photos."""
    if cv2 is None:
        alpha = Image.new("L", image.size, 0)
        margin_x, margin_y = round(image.width * 0.08), round(image.height * 0.05)
        ImageDraw.Draw(alpha).rounded_rectangle(
            (margin_x, margin_y, image.width - margin_x, image.height - margin_y),
            radius=24,
            fill=255,
        )
        result = image.convert("RGBA")
        result.putalpha(alpha)
        return result

    source = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    mask = np.full(source.shape[:2], cv2.GC_PR_BGD, np.uint8)
    margin_x, margin_y = round(image.width * 0.05), round(image.height * 0.04)
    rect = (margin_x, margin_y, image.width - 2 * margin_x, image.height - 2 * margin_y)
    mask[margin_y : image.height - margin_y, margin_x : image.width - margin_x] = cv2.GC_PR_FGD
    inner = (round(image.width * 0.14), round(image.height * 0.10), round(image.width * 0.72), round(image.height * 0.80))
    mask[inner[1] : inner[1] + inner[3], inner[0] : inner[0] + inner[2]] = cv2.GC_FGD
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(source, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    alpha = cv2.GaussianBlur(alpha, (0, 0), 1.2)
    result = image.convert("RGBA")
    result.putalpha(Image.fromarray(alpha, "L"))
    return result


def extract_subject(image: Image.Image) -> Image.Image:
    result = _rembg_cutout(image)
    return result if result is not None else _grabcut_cutout(image)


def _trim_alpha(image: Image.Image, padding: int = 4) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return image
    x0, y0, x1, y1 = bbox
    return image.crop((max(0, x0 - padding), max(0, y0 - padding), min(image.width, x1 + padding), min(image.height, y1 + padding)))


def _place_cutout(background: Image.Image, subject: Image.Image) -> Image.Image:
    canvas = background.convert("RGBA")
    subject = _trim_alpha(subject)
    max_width = round(canvas.width * 0.86)
    max_height = round(canvas.height * 0.66)
    scale = min(max_width / subject.width, max_height / subject.height)
    resized = subject.resize((max(1, round(subject.width * scale)), max(1, round(subject.height * scale))), Image.Resampling.LANCZOS)
    x = (canvas.width - resized.width) // 2
    y = round(canvas.height * 0.30)
    y = min(max(0, y), canvas.height - resized.height - round(canvas.height * 0.04))

    shadow_alpha = resized.getchannel("A").filter(ImageFilter.GaussianBlur(max(8, round(min(canvas.size) * 0.018))))
    shadow_alpha = ImageEnhance.Brightness(shadow_alpha).enhance(0.30)
    shadow_layer = Image.new("L", canvas.size, 0)
    shadow_layer.paste(shadow_alpha, (x + round(canvas.width * 0.015), y + round(canvas.height * 0.025)))
    shadow = Image.new("RGBA", canvas.size, (13, 8, 4, 0))
    shadow.putalpha(shadow_layer)
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(resized, (x, y))
    return canvas.convert("RGB")


def _looks_like_flat_template(template: Image.Image) -> bool:
    rgb = np.asarray(template.convert("RGB"), dtype=np.float32)
    neutral_white = ((rgb.min(axis=2) > 225) & ((rgb.max(axis=2) - rgb.min(axis=2)) < 22)).mean()
    return bool(neutral_white > 0.42)


def compose(
    product_path: str | Path,
    template_path: str | Path,
    output_path: str | Path,
    *,
    mode: Mode = "auto",
    size: tuple[int, int] | str | None = None,
    preserve_logo_pill: bool = True,
    outline_strength: float = 0.78,
    quality: int = 95,
) -> Path:
    """Compose one product/template pair and return the output path."""
    if mode not in {"auto", "overlay", "cutout"}:
        raise ValueError("mode 必须是 auto、overlay 或 cutout")
    product = _open_rgb(product_path)
    template = _open_rgb(template_path)
    output_size = _parse_size(size, template.size)
    background = fit_cover(product, output_size)
    selected = "overlay" if mode == "auto" and _looks_like_flat_template(template) else mode
    if selected == "auto":
        selected = "cutout"
    if selected == "overlay":
        result = _apply_overlay(background, template, preserve_logo_pill=preserve_logo_pill, outline_strength=outline_strength)
    else:
        result = _place_cutout(fit_cover(template, output_size), extract_subject(product))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        result.save(destination, format="JPEG", quality=max(1, min(100, quality)), optimize=True, subsampling=0)
    else:
        result.save(destination, format="PNG", optimize=True)
    return destination


def compose_many(
    product_paths: Sequence[str | Path] | Iterable[str | Path],
    template_path: str | Path,
    output_dir: str | Path,
    **kwargs,
) -> list[Path]:
    """Compose several product images using one template."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for product_path in product_paths:
        stem = Path(product_path).stem
        output = destination / f"{stem}_composite.jpg"
        results.append(compose(product_path, template_path, output, **kwargs))
    return results
