from __future__ import annotations

import io
import logging
import math
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

try:  # OpenCV powers placeholder detection and the cutout fallback.
    import cv2
except Exception:  # pragma: no cover - optional at import time
    cv2 = None

LOGGER = logging.getLogger(__name__)
Mode = Literal["auto", "overlay", "cutout"]
_REMBG_SESSION = None
MAX_JPEG_BYTES = 300_000
JPEG_QUALITY_FLOOR = 60


def _open_rgb(path: str | Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    """Encode a progressive JPEG with a good size/detail balance."""
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=max(1, min(100, int(quality))),
        optimize=True,
        progressive=True,
        # 4:2:2 keeps colored logo edges cleaner than 4:2:0 while saving
        # considerably more space than 4:4:4.
        subsampling=1,
    )
    return buffer.getvalue()


def _search_jpeg_quality(image: Image.Image, low: int, high: int, limit: int) -> bytes | None:
    """Return the highest JPEG quality that fits ``limit`` bytes."""
    best: bytes | None = None
    left, right = max(1, low), min(100, high)
    while left <= right:
        quality = (left + right) // 2
        encoded = _encode_jpeg(image, quality)
        if len(encoded) < limit:
            best = encoded
            left = quality + 1
        else:
            right = quality - 1
    return best


def _jpeg_under_limit(image: Image.Image, requested_quality: int, limit: int = MAX_JPEG_BYTES) -> tuple[Image.Image, bytes]:
    """Keep JPEG output below the download limit without geometric distortion.

    Quality is reduced first while preserving the requested canvas dimensions.
    If a particularly detailed food photo still exceeds the limit at the
    quality floor, the canvas is downscaled proportionally and quality is
    searched again. This avoids the blocky artifacts caused by forcing a very
    low JPEG quality at the original pixel dimensions.
    """
    requested = max(1, min(100, int(requested_quality)))
    floor = min(JPEG_QUALITY_FLOOR, requested)
    working = image.convert("RGB")

    for _ in range(12):
        floor_encoded = _encode_jpeg(working, floor)
        if len(floor_encoded) < limit:
            best = _search_jpeg_quality(working, floor, requested, limit)
            return working, best or floor_encoded

        # JPEG size roughly follows pixel area. Shrinking by the square root
        # of the required ratio keeps the next pass close to the target while
        # retaining the highest possible quality.
        ratio = math.sqrt(limit / max(1, len(floor_encoded))) * 0.96
        ratio = min(0.92, max(0.58, ratio))
        width = max(96, round(working.width * ratio))
        height = max(96, round(working.height * ratio))
        if (width, height) == working.size:
            break
        working = working.resize((width, height), Image.Resampling.LANCZOS)

    # Last-resort guard for unusually noisy images. The proportional resize
    # above normally succeeds before this point, so this path is rare.
    for _ in range(8):
        encoded = _encode_jpeg(working, max(35, floor))
        if len(encoded) < limit:
            best = _search_jpeg_quality(working, 20, max(35, floor), limit)
            return working, best or encoded
        width = max(96, round(working.width * 0.78))
        height = max(96, round(working.height * 0.78))
        if (width, height) == working.size:
            break
        working = working.resize((width, height), Image.Resampling.LANCZOS)

    encoded = _encode_jpeg(working, 20)
    while len(encoded) >= limit and min(working.size) > 96:
        width = max(96, round(working.width * 0.75))
        height = max(96, round(working.height * 0.75))
        working = working.resize((width, height), Image.Resampling.LANCZOS)
        encoded = _encode_jpeg(working, 20)
    return working, encoded


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
    """Remove a light background without punching holes through white lettering.

    A simple color threshold treats the white fill inside outlined characters as
    background. Instead, only white pixels connected to the canvas boundary are
    removed; enclosed white pixels remain part of the artwork. This is useful
    for campaign type such as white coupon lettering with a colored outline.
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    min_channel = rgb.min(axis=2)
    chroma = rgb.max(axis=2) - min_channel
    white_like = (min_channel > threshold - softness * 0.55) & (chroma < 24)

    # Find only the white regions that touch the canvas boundary. OpenCV is
    # already an optional dependency for the cutout fallback; the small flood
    # fill below keeps this path dependency-light when it is unavailable.
    border_connected = np.zeros(white_like.shape, dtype=bool)
    if cv2 is not None:
        count, labels = cv2.connectedComponents(white_like.astype(np.uint8), 8)
        border_labels = np.unique(np.concatenate((
            labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1],
        )))
        border_connected = np.isin(labels, border_labels[border_labels != 0])
    else:
        height, width = white_like.shape
        pending: list[tuple[int, int]] = []
        def add_seed(y: int, x: int) -> None:
            if white_like[y, x] and not border_connected[y, x]:
                border_connected[y, x] = True
                pending.append((y, x))
        for x in range(width):
            add_seed(0, x)
            add_seed(height - 1, x)
        for y in range(1, height - 1):
            add_seed(y, 0)
            add_seed(y, width - 1)
        while pending:
            y, x = pending.pop()
            for py, px in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= py < height and 0 <= px < width and white_like[py, px] and not border_connected[py, px]:
                    border_connected[py, px] = True
                    pending.append((py, px))

    # Artwork is opaque, including enclosed white type. Boundary-connected
    # whites fade to transparent with a short anti-aliased transition.
    alpha = np.where(border_connected, 0.0, 1.0)
    near_white = np.clip((threshold - min_channel) / max(softness, 1), 0, 1)
    alpha = np.where(border_connected, near_white, alpha)
    return Image.fromarray(np.uint8(alpha * 255)).filter(ImageFilter.GaussianBlur(0.22))


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


def _green_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Locate a large chroma-green placeholder in a template.

    Templates use green as a deliberate replacement surface. We detect it in
    HSV space instead of using a single RGB value so JPEG compression and
    slightly uneven fabric lighting do not leave a green fringe around the
    inserted product.
    """
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    if cv2 is not None:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        hue, saturation, value = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        mask = (
            (hue >= 35)
            & (hue <= 90)
            & (saturation >= 90)
            & (value >= 70)
        ).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if count <= 1:
            return None
        candidates = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            if area >= rgb.shape[0] * rgb.shape[1] * 0.012:
                candidates.append((area, index, x, y, width, height))
        if not candidates:
            return None
        _, index, x, y, width, height = max(candidates)
        if width < rgb.shape[1] * 0.25 or height < rgb.shape[0] * 0.16:
            return None
        component = np.where(labels == index, 255, 0).astype(np.uint8)
        # A small dilation covers anti-aliased green pixels at the edge while
        # the blur keeps the replacement edge natural on compressed templates.
        component = cv2.dilate(component, np.ones((3, 3), np.uint8), iterations=1)
        alpha = cv2.GaussianBlur(component, (0, 0), 1.15)
    else:
        red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
        mask = ((green > 105) & (green > red * 1.22) & (green > blue * 1.22) & (green - np.maximum(red, blue) > 28)).astype(np.uint8)
        ys, xs = np.where(mask > 0)
        if len(xs) < rgb.shape[0] * rgb.shape[1] * 0.012:
            return None
        x, y, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        width, height = x1 - x, y1 - y
        if width < rgb.shape[1] * 0.25 or height < rgb.shape[0] * 0.16:
            return None
        alpha = Image.fromarray(mask * 255, "L").filter(ImageFilter.GaussianBlur(1.15))

    bbox = (int(x), int(y), int(x + width), int(y + height))
    alpha_image = alpha if isinstance(alpha, Image.Image) else Image.fromarray(alpha, "L")
    return bbox, alpha_image


def _neutral_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Locate a large, light neutral panel used as a product placeholder."""
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    if cv2 is not None:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        # White and pale-gray panels are low-chroma and bright. Requiring
        # interior placement prevents the entire white page from becoming a
        # product slot in ordinary overlay templates.
        mask = ((hsv[:, :, 1] <= 42) & (hsv[:, :, 2] >= 205)).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        height, width = mask.shape
        for index in range(1, count):
            x, y, box_width, box_height, area = stats[index]
            if area < width * height * 0.035:
                continue
            if x <= 2 or y <= 2 or x + box_width >= width - 2 or y + box_height >= height - 2:
                continue
            rectangularity = area / max(1, box_width * box_height)
            if box_width < width * 0.22 or box_height < height * 0.22 or rectangularity < 0.70:
                continue
            candidates.append((area, index, x, y, box_width, box_height))
        if not candidates:
            return None
        _, index, x, y, box_width, box_height = max(candidates)
        component = np.where(labels == index, 255, 0).astype(np.uint8)
        alpha = cv2.GaussianBlur(component, (0, 0), 1.0)
    else:
        mask = ((rgb.min(axis=2) > 205) & ((rgb.max(axis=2) - rgb.min(axis=2)) < 32)).astype(np.uint8)
        ys, xs = np.where(mask > 0)
        if len(xs) < rgb.shape[0] * rgb.shape[1] * 0.035:
            return None
        x, y, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        box_width, box_height = x1 - x, y1 - y
        height, width = mask.shape
        if x <= 2 or y <= 2 or x1 >= width - 2 or y1 >= height - 2:
            return None
        if box_width < width * 0.22 or box_height < height * 0.22:
            return None
        alpha = Image.fromarray(mask * 255, "L").filter(ImageFilter.GaussianBlur(1.0))

    bbox = (int(x), int(y), int(x + box_width), int(y + box_height))
    alpha_image = alpha if isinstance(alpha, Image.Image) else Image.fromarray(alpha, "L")
    return bbox, alpha_image


def _product_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a green-screen or neutral placeholder suitable for product art."""
    return _green_slot(background) or _neutral_slot(background)


def _place_product_slot(background: Image.Image, product: Image.Image) -> Image.Image | None:
    """Replace a detected product slot with the original product image.

    Product panels are replacement surfaces rather than scene backgrounds.
    Keep product pixels sharp and untouched apart from an aspect-safe ``cover``
    resize into the detected slot. The only softness is the 1 px anti-aliased
    edge used to hide JPEG and template boundary noise.
    """
    detected = _product_slot(background)
    if detected is None:
        return None
    (x0, y0, x1, y1), slot_alpha = detected
    slot_width, slot_height = x1 - x0, y1 - y0
    if slot_width < 2 or slot_height < 2:
        return None

    canvas = background.convert("RGBA")
    # ``fit_cover`` preserves the food's aspect ratio and uses LANCZOS with a
    # mild downscale sharpen. No blur or second composited copy is introduced.
    source_plate = fit_cover(product.convert("RGB"), (slot_width, slot_height))
    plate_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    plate_layer.paste(source_plate.convert("RGBA"), (x0, y0))
    slot_overlay = Image.new("L", canvas.size, 0)
    slot_overlay.paste(slot_alpha, (0, 0))
    plate_layer.putalpha(slot_overlay)
    return Image.alpha_composite(canvas, plate_layer).convert("RGB")


def _place_cutout(background: Image.Image, subject: Image.Image | None = None, product: Image.Image | None = None) -> Image.Image:
    if product is not None:
        slot_result = _place_product_slot(background, product)
        if slot_result is not None:
            return slot_result
    if subject is None and product is not None:
        subject = extract_subject(product)
    if subject is None:
        raise ValueError("主体抠图需要商品图")
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
    quality: int = 100,
) -> Path:
    """Compose one product/template pair and return the output path."""
    if mode not in {"auto", "overlay", "cutout"}:
        raise ValueError("mode 必须是 auto、overlay 或 cutout")
    product = _open_rgb(product_path)
    template = _open_rgb(template_path)
    output_size = _parse_size(size, template.size)
    background = fit_cover(product, output_size)
    template_page = fit_cover(template, output_size)
    if mode == "auto":
        selected = "cutout" if _product_slot(template_page) is not None else "overlay" if _looks_like_flat_template(template) else "cutout"
    else:
        selected = mode
    if selected == "overlay":
        result = _apply_overlay(background, template, preserve_logo_pill=preserve_logo_pill, outline_strength=outline_strength)
    else:
        result = _place_cutout(template_page, product=product)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        _, encoded = _jpeg_under_limit(result, quality)
        destination.write_bytes(encoded)
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
