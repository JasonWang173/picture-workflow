from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

try:  # OpenCV powers placeholder detection and the cutout fallback.
    import cv2
except Exception:  # pragma: no cover - optional at import time
    cv2 = None

LOGGER = logging.getLogger(__name__)
Mode = Literal["auto", "overlay", "cutout", "template_replace"]
_REMBG_SESSION = None
MAX_JPEG_BYTES = 300_000
JPEG_QUALITY_FLOOR = 60
JPEG_LUMA_PRIORITY_CHROMA_SCALE_MAX = 3.0


def _open_rgb(path: str | Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _open_template(path: str | Path) -> Image.Image:
    """Open a template without flattening its native PNG transparency."""
    with Image.open(path) as image:
        return image.convert("RGBA")


def _has_transparency(image: Image.Image) -> bool:
    """Whether an image contains any non-opaque alpha pixels."""
    if "A" not in image.getbands():
        return False
    return image.getchannel("A").getextrema()[0] < 255


def _encode_jpeg(image: Image.Image, quality: int, subsampling: int = 1) -> bytes:
    """Encode a progressive JPEG with a good size/detail balance."""
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=max(1, min(100, int(quality))),
        optimize=True,
        progressive=True,
        # 4:4:4 is tried first for crisp type; 4:2:2/4:2:0 are fallbacks when
        # the fixed canvas needs more compression to stay under the limit.
        subsampling=subsampling,
    )
    return buffer.getvalue()


def _search_jpeg_quality(image: Image.Image, low: int, high: int, limit: int, subsampling: int) -> bytes | None:
    """Return the highest JPEG quality that fits ``limit`` bytes."""
    result = _search_jpeg_quality_level(image, low, high, limit, subsampling)
    return result[1] if result is not None else None


def _search_jpeg_quality_level(
    image: Image.Image,
    low: int,
    high: int,
    limit: int,
    subsampling: int,
) -> tuple[int, bytes] | None:
    """Return the highest fitting quality together with its encoded bytes."""
    best: bytes | None = None
    best_quality: int | None = None
    left, right = max(1, low), min(100, high)
    while left <= right:
        quality = (left + right) // 2
        encoded = _encode_jpeg(image, quality, subsampling)
        if len(encoded) < limit:
            best = encoded
            best_quality = quality
            left = quality + 1
        else:
            right = quality - 1
    return (best_quality, best) if best is not None and best_quality is not None else None


def _encode_luma_priority_jpeg(image: Image.Image, quality: int, chroma_scale: float) -> bytes:
    """Encode JPEG with a higher luma quality and a controlled chroma budget.

    At a fixed 1080x1920 canvas, a normal JPEG quality search can spend too
    many bytes on chroma detail in the food photo and then lower the luma table
    enough to soften Chinese type and thin outlines. Reusing Pillow's standard
    luma table at a higher quality while scaling only the chroma table keeps
    edges/text sharper without changing dimensions or the JPG format.
    """
    probe = _encode_jpeg(image, quality, subsampling=2)
    with Image.open(io.BytesIO(probe)) as decoded:
        tables = decoded.quantization
    if not tables or 0 not in tables or 1 not in tables:
        return probe
    qtables = {
        0: tables[0],
        1: [min(255, max(1, round(value * chroma_scale))) for value in tables[1]],
    }
    buffer = io.BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        qtables=qtables,
        optimize=True,
        progressive=True,
        subsampling=2,
    )
    return buffer.getvalue()


def _search_luma_priority_jpeg(
    image: Image.Image,
    low: int,
    high: int,
    limit: int,
) -> tuple[int, bytes] | None:
    """Find a luma-priority candidate that remains below the byte limit."""
    max_scale = JPEG_LUMA_PRIORITY_CHROMA_SCALE_MAX

    def fits(quality: int, scale: float) -> bytes | None:
        encoded = _encode_luma_priority_jpeg(image, quality, scale)
        return encoded if len(encoded) < limit else None

    # First maximize luma quality with the bounded chroma trade-off.
    best_quality: int | None = None
    best_at_max_scale: bytes | None = None
    left, right = max(1, low), min(100, high)
    while left <= right:
        quality = (left + right) // 2
        encoded = fits(quality, max_scale)
        if encoded is not None:
            best_quality = quality
            best_at_max_scale = encoded
            left = quality + 1
        else:
            right = quality - 1
    if best_quality is None or best_at_max_scale is None:
        return None

    # For that luma quality, use the least chroma reduction that fits. This
    # prevents the optimization from needlessly desaturating ordinary photos.
    minimum = fits(best_quality, 1.0)
    if minimum is not None:
        return best_quality, minimum
    left_scale, right_scale = 1.0, max_scale
    best = best_at_max_scale
    for _ in range(8):
        scale = (left_scale + right_scale) / 2.0
        encoded = fits(best_quality, scale)
        if encoded is None:
            left_scale = scale
        else:
            best = encoded
            right_scale = scale
    return best_quality, best


def _jpeg_under_limit(image: Image.Image, requested_quality: int, limit: int = MAX_JPEG_BYTES) -> tuple[Image.Image, bytes]:
    """Keep JPEG output below the download limit at the exact canvas size.

    The image dimensions are immutable here. We first try 4:4:4 for the
    clearest text and colored edges, then use 4:2:2/4:2:0 only when the fixed
    canvas needs more compact encoding. Quality is searched rather than picked
    as a blunt constant, so ordinary food photos retain the best possible
    quality under the limit. For the compact 4:2:0 fallback, a luma-priority
    table can keep text and edges at a higher quality while spending fewer
    bytes on chroma detail.
    """
    requested = max(1, min(100, int(requested_quality)))
    floor = min(JPEG_QUALITY_FLOOR, requested)
    working = image.convert("RGB")
    for subsampling in (0, 1, 2):
        floor_encoded = _encode_jpeg(working, floor, subsampling)
        if len(floor_encoded) < limit:
            standard = _search_jpeg_quality_level(working, floor, requested, limit, subsampling)
            if standard is None:
                return working, floor_encoded
            if subsampling == 2:
                luma_priority = _search_luma_priority_jpeg(working, floor, requested, limit)
                # Require a meaningful luma-quality gain before trading away
                # additional chroma detail. Otherwise retain the ordinary
                # highest-quality JPEG candidate.
                if luma_priority is not None and luma_priority[0] >= standard[0] + 3:
                    return working, luma_priority[1]
            return working, standard[1]

    # Fixed-size output takes priority over keeping a high quality value for a
    # pathological, highly noisy image. Search the remaining quality range in
    # the most compact chroma mode before failing loudly instead of changing
    # the requested canvas dimensions.
    standard = _search_jpeg_quality_level(working, 1, floor, limit, subsampling=2)
    luma_priority = _search_luma_priority_jpeg(working, 1, floor, limit)
    if luma_priority is not None and (standard is None or luma_priority[0] >= standard[0] + 3):
        return working, luma_priority[1]
    if standard is not None:
        return working, standard[1]
    encoded = _encode_jpeg(working, 1, subsampling=2)
    if len(encoded) >= limit:
        raise ValueError("图片在保持选定尺寸时无法压缩到 300KB 以下")
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


def _edge_fill_color(image: Image.Image) -> tuple[int, int, int]:
    """Estimate a stable page color for the extra canvas around a template."""
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    border = np.concatenate((rgba[0], rgba[-1], rgba[:, 0], rgba[:, -1]), axis=0)
    opaque = border[border[:, 3] >= 192, :3]
    if opaque.size == 0:
        return (255, 255, 255)
    return tuple(int(value) for value in np.median(opaque, axis=0))


def fit_contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Fit the complete template inside a canvas without cropping or stretching.

    A vertical downloaded template may be exported to a horizontal preset (or
    the reverse).  Extending the template's edge color keeps the whole layout
    visible and avoids the severe headline/frame cropping caused by ``cover``.
    """
    width, height = size
    if image.size == size:
        return image.copy()
    scale = min(width / image.width, height / image.height)
    resized_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(resized_size, Image.Resampling.LANCZOS)
    if scale < 0.98:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=0.45, percent=105, threshold=3))
    fill = _edge_fill_color(image)
    canvas = Image.new("RGBA", size, (*fill, 255))
    left = (width - resized.width) // 2
    top = (height - resized.height) // 2
    canvas.alpha_composite(resized.convert("RGBA"), (left, top))
    return canvas


def _connected_to_border(mask: np.ndarray) -> np.ndarray:
    """Return only mask pixels that are connected to the canvas boundary."""
    if cv2 is not None:
        count, labels = cv2.connectedComponents(mask.astype(np.uint8), 8)
        border_labels = np.unique(np.concatenate((
            labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1],
        )))
        return np.isin(labels, border_labels[border_labels != 0])

    height, width = mask.shape
    connected = np.zeros_like(mask, dtype=bool)
    pending: list[tuple[int, int]] = []

    def add_seed(y: int, x: int) -> None:
        if mask[y, x] and not connected[y, x]:
            connected[y, x] = True
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
            if 0 <= py < height and 0 <= px < width and mask[py, px] and not connected[py, px]:
                connected[py, px] = True
                pending.append((py, px))
    return connected


def _edge_background_alpha(image: Image.Image) -> Image.Image | None:
    """Remove a flat edge-connected background while keeping enclosed artwork.

    Templates are commonly exported with either a black or white page. The
    previous implementation only keyed white, so a black template was treated
    as an opaque foreground and hid the product photo. We estimate the page
    color from all four borders, flood-fill only pixels close to that color, and
    leave enclosed white fills (or other artwork) untouched.
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    background = np.median(border, axis=0)
    # A photographic border has too much color/lighting variation to be safely
    # keyed. Flat campaign pages usually have >85% of border pixels near one
    # color, even after JPEG compression.
    border_delta = np.max(np.abs(border - background), axis=1)
    if float(np.mean(border_delta <= 32.0)) < 0.82:
        return None

    distance = np.max(np.abs(rgb - background), axis=2)
    # Keep the key tight for dark pages (black outlines are common) and a bit
    # wider for white JPEG pages where compression creates gray fringes.
    is_dark = float(np.mean(background)) < 90.0
    tolerance = 34.0 if is_dark else 42.0
    candidate = distance <= tolerance
    connected = _connected_to_border(candidate)
    if float(connected.mean()) < 0.12:
        return None

    # Anti-aliased transition only at the boundary of the removed component.
    # This avoids a gray halo while retaining the original RGB values of the
    # template artwork and the product photo underneath.
    alpha = np.where(connected, np.clip((distance - tolerance * 0.55) / (tolerance * 0.45), 0.0, 1.0), 1.0)
    return Image.fromarray(np.uint8(alpha * 255)).filter(ImageFilter.GaussianBlur(0.22))


def _soft_white_alpha(image: Image.Image, threshold: float = 235.0, softness: float = 23.0) -> Image.Image:
    """Backward-compatible white-background key used by older callers."""
    alpha = _edge_background_alpha(image)
    if alpha is not None:
        return alpha
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    min_channel = rgb.min(axis=2)
    chroma = rgb.max(axis=2) - min_channel
    white_like = (min_channel > threshold - softness * 0.55) & (chroma < 24)
    border_connected = _connected_to_border(white_like)
    alpha_array = np.where(border_connected, np.clip((threshold - min_channel) / max(softness, 1), 0, 1), 1.0)
    return Image.fromarray(np.uint8(alpha_array * 255)).filter(ImageFilter.GaussianBlur(0.22))


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
    # A PNG template's alpha channel is authoritative. The RGB values behind
    # transparent pixels are often black (or a checkerboard preview), so
    # flattening it before compositing recreates the exact bug this path fixes.
    uses_native_alpha = _has_transparency(page)
    if uses_native_alpha:
        alpha = page.getchannel("A")
    else:
        # For legacy JPG templates, prefer a black/white/neutral edge key. The
        # fallback preserves the original white-key behavior for unusual ones.
        alpha = _edge_background_alpha(page)
        if alpha is None:
            alpha = _soft_white_alpha(page)

    # Native-alpha templates already contain the designer's exact pill and
    # edge treatment. Do not enlarge or redraw either of them.
    if preserve_logo_pill and not uses_native_alpha:
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
    if outline_strength > 0.01 and not uses_native_alpha:
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
    opaque = None
    if "A" in background.getbands():
        opaque = np.asarray(background.getchannel("A"), dtype=np.uint8) >= 12
    if cv2 is not None:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        hue, saturation, value = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        mask = (
            (hue >= 35)
            & (hue <= 90)
            & (saturation >= 90)
            & (value >= 70)
        ).astype(np.uint8)
        if opaque is not None:
            mask = (mask & opaque).astype(np.uint8)
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
        # Keep the detected edge hard.  Product insertion applies its own
        # one-pixel inward anti-aliasing so green pixels cannot bleed across a
        # photo frame or become a fuzzy halo.
        alpha = component
    else:
        red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
        mask = ((green > 105) & (green > red * 1.22) & (green > blue * 1.22) & (green - np.maximum(red, blue) > 28)).astype(np.uint8)
        if opaque is not None:
            mask = (mask & opaque).astype(np.uint8)
        ys, xs = np.where(mask > 0)
        if len(xs) < rgb.shape[0] * rgb.shape[1] * 0.012:
            return None
        x, y, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        width, height = x1 - x, y1 - y
        if width < rgb.shape[1] * 0.25 or height < rgb.shape[0] * 0.16:
            return None
        alpha = Image.fromarray(mask * 255, "L")

    bbox = (int(x), int(y), int(x + width), int(y + height))
    alpha_image = alpha if isinstance(alpha, Image.Image) else Image.fromarray(alpha, "L")
    return bbox, alpha_image


def _neutral_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Locate a large, light neutral panel used as a product placeholder."""
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    opaque = None
    if "A" in background.getbands():
        opaque = np.asarray(background.getchannel("A"), dtype=np.uint8) >= 12
    if cv2 is not None:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        # White and pale-gray panels are low-chroma and bright. Requiring
        # interior placement prevents the entire white page from becoming a
        # product slot in ordinary overlay templates.
        mask = ((hsv[:, :, 1] <= 42) & (hsv[:, :, 2] >= 205)).astype(np.uint8)
        if opaque is not None:
            mask = (mask & opaque).astype(np.uint8)
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
        alpha = component
    else:
        mask = ((rgb.min(axis=2) > 205) & ((rgb.max(axis=2) - rgb.min(axis=2)) < 32)).astype(np.uint8)
        if opaque is not None:
            mask = (mask & opaque).astype(np.uint8)
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
        alpha = Image.fromarray(mask * 255, "L")

    bbox = (int(x), int(y), int(x + box_width), int(y + box_height))
    alpha_image = alpha if isinstance(alpha, Image.Image) else Image.fromarray(alpha, "L")
    return bbox, alpha_image


def _product_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a green-screen or neutral placeholder suitable for product art."""
    return _green_slot(background) or _neutral_slot(background)


def _crisp_slot_alpha(slot_alpha: Image.Image, inset: int = 1) -> Image.Image:
    """Prepare a replacement mask without the blurred halo that causes spill.

    Detection masks are intentionally softened in older versions.  That makes
    JPEG edges look friendly, but it also lets the old green/photo pixels show
    through the inserted product.  A tiny erosion keeps the frame border in
    front; a sub-pixel blur retains a clean anti-aliased edge.
    """
    alpha = np.asarray(slot_alpha.convert("L"), dtype=np.uint8)
    binary = (alpha >= 128).astype(np.uint8) * 255
    if inset <= 0:
        return Image.fromarray(binary, "L")
    if inset > 0 and cv2 is not None:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (inset * 2 + 1, inset * 2 + 1))
        binary = cv2.erode(binary, kernel, iterations=1)
    return Image.fromarray(binary, "L").filter(ImageFilter.GaussianBlur(0.28))


def _fit_cover_mask(mask: Image.Image, size: tuple[int, int], focal: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    """Apply the same cover scale/crop as ``fit_cover`` to a grayscale mask."""
    width, height = size
    if mask.size == size:
        return mask.copy()
    scale = max(width / mask.width, height / mask.height)
    resized = mask.resize((round(mask.width * scale), round(mask.height * scale)), Image.Resampling.LANCZOS)
    left = round((resized.width - width) * min(max(focal[0], 0), 1))
    top = round((resized.height - height) * min(max(focal[1], 0), 1))
    return resized.crop((left, top, left + width, top + height))


def _fit_contain_mask(mask: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Apply the same contain scale/offset as ``fit_contain`` to a mask."""
    width, height = size
    if mask.size == size:
        return mask.copy()
    scale = min(width / mask.width, height / mask.height)
    resized = mask.resize(
        (max(1, round(mask.width * scale)), max(1, round(mask.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("L", size, 0)
    canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return canvas


def _slot_for_cover(
    detected: tuple[tuple[int, int, int, int], Image.Image],
    size: tuple[int, int],
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Map a native-template slot through the same cover transform as the page."""
    _, native_mask = detected
    mapped_mask = _fit_cover_mask(native_mask, size)
    bbox = mapped_mask.getbbox()
    if not bbox:
        return None
    return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])), mapped_mask


def _slot_for_contain(
    detected: tuple[tuple[int, int, int, int], Image.Image],
    size: tuple[int, int],
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Map a native-template slot through the same contain transform as the page."""
    _, native_mask = detected
    mapped_mask = _fit_contain_mask(native_mask, size)
    bbox = mapped_mask.getbbox()
    if not bbox:
        return None
    return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])), mapped_mask


def _transparent_product_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a large interior transparent hole in a PNG template.

    Transparent template pixels are intentional replacement slots, not black
    content. We reject components touching the canvas edge so a transparent
    page background cannot be mistaken for a product frame.
    """
    if "A" not in background.getbands():
        return None
    alpha = np.asarray(background.getchannel("A"), dtype=np.uint8)
    mask = (alpha <= 12).astype(np.uint8)
    height, width = mask.shape
    if cv2 is not None:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        for index in range(1, count):
            x, y, box_width, box_height, area = stats[index]
            if area < width * height * 0.04:
                continue
            if x <= 2 or y <= 2 or x + box_width >= width - 2 or y + box_height >= height - 2:
                continue
            if box_width < width * 0.25 or box_height < height * 0.25:
                continue
            rectangularity = area / max(1, box_width * box_height)
            if rectangularity < 0.78:
                continue
            candidates.append((area, index, x, y, box_width, box_height))
        if not candidates:
            return None
        _, index, x, y, box_width, box_height = max(candidates)
        component = np.where(labels == index, 255, 0).astype(np.uint8)
        return (int(x), int(y), int(x + box_width), int(y + box_height)), Image.fromarray(component, "L")

    # Dependency-light fallback: only accept a clean interior bounding box.
    ys, xs = np.where(mask > 0)
    if len(xs) < width * height * 0.04:
        return None
    x, y, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    box_width, box_height = x1 - x, y1 - y
    if x <= 2 or y <= 2 or x1 >= width - 2 or y1 >= height - 2:
        return None
    if box_width < width * 0.25 or box_height < height * 0.25:
        return None
    return (int(x), int(y), int(x1), int(y1)), Image.fromarray(mask * 255, "L")


def _framed_photo_slot(
    background: Image.Image,
    *,
    preserve_frame: bool = True,
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a large axis-aligned photo frame already baked into a JPG template."""
    if cv2 is None:
        return None
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 45, 130)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    # Keep a small amount of visual information with each rectangle.  A
    # number of vertical posters put the product photo inside a much larger
    # white card that also contains the headline and CTA.  The old selector
    # ranked only by area and consequently selected that whole card.  The
    # bright/low-chroma ratio below is a useful, layout-independent signal for
    # such a text card: real food photos have texture and colour, while the
    # surrounding copy area is mostly near-white.
    candidates = []
    for contour in contours:
        x, y, box_width, box_height = cv2.boundingRect(contour)
        box_area = box_width * box_height
        if box_area < width * height * 0.12:
            continue
        if x <= width * 0.015 or y <= height * 0.015 or x + box_width >= width * 0.985 or y + box_height >= height * 0.985:
            continue
        if box_width < width * 0.28 or box_height < height * 0.30:
            continue
        contour_area = cv2.contourArea(contour)
        rectangularity = contour_area / max(1, box_area)
        if rectangularity < 0.82:
            continue
        perimeter = cv2.arcLength(contour, True)
        corners = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        corner_bonus = 1.15 if len(corners) == 4 else 1.0
        crop = rgb[
            min(height, y + max(2, round(box_height * 0.01))):max(
                y + max(2, round(box_height * 0.01)),
                min(height, y + box_height - max(2, round(box_height * 0.01))),
            ),
            min(width, x + max(2, round(box_width * 0.01))):max(
                x + max(2, round(box_width * 0.01)),
                min(width, x + box_width - max(2, round(box_width * 0.01))),
            ),
        ]
        if crop.size:
            hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
            bright_neutral_ratio = float(np.mean((hsv[:, :, 2] >= 225) & (hsv[:, :, 1] <= 35)))
            chroma_ratio = float(np.mean((hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 40)))
        else:
            bright_neutral_ratio, chroma_ratio = 0.0, 0.0
        candidates.append(
            (
                box_area * rectangularity * corner_bonus,
                x,
                y,
                box_width,
                box_height,
                bright_neutral_ratio,
                chroma_ratio,
            )
        )
    if not candidates:
        return None

    # If a large candidate contains a materially smaller, well-formed
    # rectangle with much less near-white area, treat the smaller rectangle as
    # the actual product photo.  This is deliberately conservative: it only
    # applies to nested rectangles and leaves ordinary horizontal photo frames
    # unchanged.
    rejected_outer: set[int] = set()
    for outer_index, outer in enumerate(candidates):
        _, ox, oy, ow, oh, outer_neutral, _ = outer
        outer_area = ow * oh
        if outer_neutral < 0.12:
            continue
        for inner_index, inner in enumerate(candidates):
            if outer_index == inner_index:
                continue
            _, ix, iy, iw, ih, inner_neutral, inner_chroma = inner
            inner_area = iw * ih
            if inner_area >= outer_area * 0.82 or inner_area <= outer_area * 0.14:
                continue
            if ix < ox + ow * 0.02 or iy < oy + oh * 0.02:
                continue
            # Contour bounds can differ by a few pixels because the outer
            # card's rounded corners and the inner photo edge are detected on
            # different antialiased rows.  Allow that small tolerance while
            # still requiring the candidate to be genuinely nested.
            if ix + iw > ox + ow * 1.01 or iy + ih > oy + oh * 1.01:
                continue
            if iy <= oy + oh * 0.18 or ih >= oh * 0.78:
                continue
            if inner_neutral <= outer_neutral * 0.55 and inner_chroma >= 0.18:
                rejected_outer.add(outer_index)
                break

    selectable = [candidate for index, candidate in enumerate(candidates) if index not in rejected_outer]
    _, x, y, box_width, box_height, _, _ = max(selectable or candidates)
    # A detected contour includes the template's decorative frame. Keep a
    # small, scale-aware ring for that frame and replace only the photo inside
    # it. A chroma-key panel detected later is still replaced edge-to-edge.
    inset = max(2, round(min(box_width, box_height) * 0.006)) if preserve_frame else 0
    x0, y0 = x + inset, y + inset
    x1, y1 = x + box_width - inset, y + box_height - inset
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return (x0, y0, x1, y1), mask


def _line_framed_photo_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a soft rectangular photo panel when the source has no frame line.

    Downloaded campaign JPGs often have a product photo blended directly into
    the surrounding artwork.  The four panel edges are still visible as long
    low-contrast lines, so a multi-threshold Hough pass is a safer fallback
    than treating the whole template as a product image.
    """
    if cv2 is None:
        return None
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    horizontal: list[tuple[int, int, int, float]] = []
    vertical: list[tuple[int, int, int, float]] = []
    for low, high in ((5, 30), (10, 45), (15, 60), (20, 80), (30, 100)):
        edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), low, high)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180,
            threshold=max(25, round(width * 0.08)),
            minLineLength=max(60, round(min(width, height) * 0.22)),
            maxLineGap=max(8, round(min(width, height) * 0.02)),
        )
        if lines is None:
            continue
        for x0, y0, x1, y1 in lines:
            x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))
            dx, dy = x1 - x0, y1 - y0
            length = float((dx * dx + dy * dy) ** 0.5)
            if abs(dx) >= 5 * abs(dy):
                horizontal.append((int(round((y0 + y1) / 2)), min(x0, x1), max(x0, x1), length))
            elif abs(dy) >= 5 * abs(dx):
                vertical.append((int(round((x0 + x1) / 2)), min(y0, y1), max(y0, y1), length))

    def cluster(lines: list[tuple[int, int, int, float]]) -> list[tuple[int, int, int, float]]:
        groups: list[tuple[int, int, int, float]] = []
        for line in sorted(lines):
            if groups and abs(line[0] - groups[-1][0]) <= 4:
                previous = groups[-1]
                groups[-1] = (
                    round((previous[0] + line[0]) / 2),
                    min(previous[1], line[1]),
                    max(previous[2], line[2]),
                    max(previous[3], line[3]),
                )
            else:
                groups.append(line)
        return groups

    horizontal, vertical = cluster(horizontal), cluster(vertical)
    if len(horizontal) < 2 or len(vertical) < 2:
        return None
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 10, 45)

    def edge_support_horizontal(y: int, x0: int, x1: int) -> float:
        return float(np.mean(edges[max(0, y - 2):min(height, y + 3), x0:x1] > 0)) if x1 > x0 else 0.0

    def edge_support_vertical(x: int, y0: int, y1: int) -> float:
        return float(np.mean(edges[y0:y1, max(0, x - 2):min(width, x + 3)] > 0)) if y1 > y0 else 0.0

    def coverage(start: int, end: int, target_start: int, target_end: int) -> float:
        expected = max(1, target_end - target_start)
        overlap = max(0, min(end, target_end) - max(start, target_start))
        overshoot = max(0, target_start - start) + max(0, end - target_end)
        return (overlap / expected) * float(np.exp(-overshoot / expected))

    candidates: list[tuple[float, tuple[int, int, int, int], float]] = []
    for top in horizontal:
        for bottom in horizontal:
            y0, y1 = top[0], bottom[0]
            # Product panels in tall posters commonly extend close to the
            # bottom safe area.  Do not discard those candidates merely
            # because their bottom edge is below the old 94% cutoff.
            if y1 - y0 < height * 0.30 or y1 - y0 > height * 0.82 or y0 < height * 0.12 or y1 > height * 0.98:
                continue
            for left in vertical:
                for right in vertical:
                    x0, x1 = left[0], right[0]
                    if x1 - x0 < width * 0.28 or x1 - x0 > width * 0.95 or x0 < width * 0.02 or x1 > width * 0.98:
                        continue
                    horizontal_coverage = min(
                        coverage(top[1], top[2], x0, x1),
                        coverage(bottom[1], bottom[2], x0, x1),
                    )
                    vertical_coverage = min(
                        coverage(left[1], left[2], y0, y1),
                        coverage(right[1], right[2], y0, y1),
                    )
                    boundary_coverage = min(horizontal_coverage, vertical_coverage)
                    support = min(
                        edge_support_horizontal(y0, x0, x1),
                        edge_support_horizontal(y1, x0, x1),
                        edge_support_vertical(x0, y0, y1),
                        edge_support_vertical(x1, y0, y1),
                    )
                    if boundary_coverage < 0.48 or support < 0.12:
                        continue
                    area_ratio = ((x1 - x0) * (y1 - y0)) / (width * height)
                    # Square the horizontal coverage so an internal text or
                    # banner baseline cannot beat a complete photo-panel edge.
                    score = support * (horizontal_coverage ** 2) * vertical_coverage * area_ratio
                    candidates.append((score, (x0, y0, x1, y1), vertical_coverage))
    if not candidates:
        return None
    # Textures inside a product photo can create a second, shorter horizontal
    # line that scores almost as well as the real bottom edge.  When several
    # candidates are close in confidence, prefer the one that covers the
    # larger complete panel.  This prevents the mask from stopping halfway
    # through white packaging or the lower strip of a product card.
    best = max(candidates, key=lambda candidate: candidate[0])
    # A weak vertical edge is the signature of a photo card whose bottom
    # border is interrupted by packaging or text.  In that case a slightly
    # lower-scoring, longer candidate is usually the complete panel.  If the
    # best candidate already has solid vertical support, keep the precise
    # highest-scoring bounds so decorative footer lines cannot be included.
    if best[2] < 0.70:
        near_best = [candidate for candidate in candidates if candidate[0] >= best[0] * 0.90]
        best = max(
            near_best,
            key=lambda candidate: ((candidate[1][2] - candidate[1][0]) * (candidate[1][3] - candidate[1][1]), candidate[0]),
        )
    _, (x0, y0, x1, y1), _ = best
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1, y1), fill=255)
    return (x0, y0, x1, y1), mask


def _mask_is_green(background: Image.Image, slot_alpha: Image.Image) -> bool:
    """Classify a mapped slot so green panels receive an edge-to-edge mask."""
    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    active = np.asarray(slot_alpha.convert("L"), dtype=np.uint8) >= 128
    if not np.any(active):
        return False
    pixels = rgb[active]
    green = (pixels[:, 1] >= pixels[:, 0] + 35) & (pixels[:, 1] >= pixels[:, 2] + 35) & (pixels[:, 1] >= 95)
    return float(np.mean(green)) >= 0.55


def _is_solid_green_panel(
    background: Image.Image,
    detected: tuple[tuple[int, int, int, int], Image.Image] | None,
) -> bool:
    """Accept green only when it fills a panel, not when it is artwork noise."""
    if detected is None:
        return False
    (x0, y0, x1, y1), slot_alpha = detected
    if not _mask_is_green(background, slot_alpha):
        return False
    active = np.asarray(slot_alpha.convert("L"), dtype=np.uint8) >= 128
    if x1 <= x0 or y1 <= y0:
        return False
    occupancy = float(np.mean(active[y0:y1, x0:x1]))
    return occupancy >= 0.70


def _template_replace_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Locate the best product area for the explicit template-replace mode."""
    # A fully green panel is an explicit placeholder and has a more reliable
    # boundary than Hough lines. Prefer it when it fills a rectangular area;
    # isolated green artwork still falls through to structural detection.
    green = _green_slot(background)
    if _is_solid_green_panel(background, green):
        return green
    # Structural frame detection handles photographed panels and dark/white
    # placeholders while preserving their decorative border.
    return _transparent_product_slot(background) or _framed_photo_slot(background, preserve_frame=True) or _line_framed_photo_slot(background) or green or _product_slot(background)


def _place_template_product(
    background: Image.Image,
    product: Image.Image,
    underlay: Image.Image | None = None,
) -> Image.Image:
    """Replace a template's product frame while leaving all other pixels intact."""
    detected = _template_replace_slot(background)
    if detected is None:
        raise ValueError("未识别到模板商品图片区，请使用带明显矩形/圆角图片区的模板")
    (x0, y0, x1, y1), slot_alpha = detected
    slot_width, slot_height = x1 - x0, y1 - y0
    if slot_width < 2 or slot_height < 2:
        raise ValueError("模板商品图片区尺寸过小，无法替换")

    source_plate = fit_cover(product.convert("RGB"), (slot_width, slot_height))
    plate_layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    plate_layer.paste(source_plate.convert("RGBA"), (x0, y0))
    full_mask = Image.new("L", background.size, 0)
    edge_inset = max(1, round(min(slot_width, slot_height) * 0.0025))
    full_mask.paste(_crisp_slot_alpha(slot_alpha, edge_inset), (0, 0))
    plate_layer.putalpha(full_mask)

    if _has_transparency(background):
        # The product goes below the template so the PNG frame/border remains
        # untouched while the transparent hole reveals the replacement image.
        base = (underlay or Image.new("RGBA", background.size, (0, 0, 0, 0))).convert("RGBA")
        base.alpha_composite(plate_layer)
        base.alpha_composite(background.convert("RGBA"))
        return base.convert("RGB")

    canvas = background.convert("RGBA")
    canvas.alpha_composite(plate_layer)
    return canvas.convert("RGB")


def _remove_template_product(
    background: Image.Image,
    fill: tuple[int, int, int] = (0, 255, 0),
    detected: tuple[tuple[int, int, int, int], Image.Image] | None = None,
) -> Image.Image:
    """Remove the detected product area and return a chroma-key blank template."""
    detected = detected or _template_replace_slot(background)
    if detected is None:
        raise ValueError("未识别到模板商品图片区，请使用带明显矩形/圆角图片区的模板")
    _, slot_alpha = detected
    canvas = Image.new("RGBA", background.size, (255, 255, 255, 255))
    canvas.alpha_composite(background.convert("RGBA"))
    # A vivid green placeholder makes the result immediately usable by the
    # following ``cutout`` step, which detects this region as a replacement
    # surface. The rest of the downloaded template is untouched.
    blank = Image.new("RGBA", background.size, (*fill, 255))
    hard_alpha = Image.fromarray((np.asarray(slot_alpha.convert("L"), dtype=np.uint8) >= 128).astype(np.uint8) * 255, "L")
    blank.putalpha(hard_alpha)
    canvas.alpha_composite(blank)
    return canvas.convert("RGB")


def _place_product_slot(
    background: Image.Image,
    product: Image.Image,
    detected: tuple[tuple[int, int, int, int], Image.Image] | None = None,
) -> Image.Image | None:
    """Replace a detected product slot with the original product image.

    Product panels are replacement surfaces rather than scene backgrounds.
    Keep product pixels sharp and untouched apart from an aspect-safe ``cover``
    resize into the detected slot. The only softness is the 1 px anti-aliased
    edge used to hide JPEG and template boundary noise.
    """
    explicit_detected = detected is not None
    green_detected = _green_slot(background)
    detected = detected or green_detected or _neutral_slot(background) or _template_replace_slot(background)
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
    # A slot mapped from the native template already includes the intended
    # frame ring. Applying another erosion here creates a one pixel gap that
    # becomes a visible halo after JPEG encoding. Only heuristic neutral/photo
    # detections need the tiny safety inset.
    edge_inset = 0 if (explicit_detected or green_detected is not None or _mask_is_green(background, slot_alpha)) else max(1, round(min(slot_width, slot_height) * 0.0025))
    slot_overlay.paste(_crisp_slot_alpha(slot_alpha, edge_inset), (0, 0))
    plate_layer.putalpha(slot_overlay)
    return Image.alpha_composite(canvas, plate_layer).convert("RGB")


def _place_cutout(
    background: Image.Image,
    subject: Image.Image | None = None,
    product: Image.Image | None = None,
    detected: tuple[tuple[int, int, int, int], Image.Image] | None = None,
) -> Image.Image:
    if product is not None:
        slot_result = _place_product_slot(background, product, detected=detected)
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
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    background = np.median(border, axis=0)
    border_delta = np.max(np.abs(border - background), axis=1)
    edge_uniform = float(np.mean(border_delta <= 32.0))
    edge_color_area = float(np.mean(np.max(np.abs(rgb - background), axis=2) <= (42.0 if float(np.mean(background)) >= 90.0 else 34.0)))
    # The white test remains useful for very light templates whose border has
    # a tiny amount of paper texture. The edge test also recognizes the black
    # campaign template that previously fell through to cutout mode.
    return bool(neutral_white > 0.42 or (edge_uniform > 0.82 and edge_color_area > 0.32))


def compose(
    product_path: str | Path | None,
    template_path: str | Path,
    output_path: str | Path,
    *,
    mode: Mode = "auto",
    size: tuple[int, int] | str | None = None,
    preserve_logo_pill: bool = True,
    outline_strength: float = 0.78,
    quality: int = 100,
) -> Path:
    """Compose one product/template pair and return the output path.

    ``template_replace`` is also a product-less operation: when
    ``product_path`` is omitted, the detected product panel is cleared and a
    blank template is written.  Other modes still require a product image.
    """
    if mode not in {"auto", "overlay", "cutout", "template_replace"}:
        raise ValueError("mode 必须是 auto、overlay、cutout 或 template_replace")
    template = _open_template(template_path)
    output_size = _parse_size(size, template.size)
    # Template replacement and green-slot composition preserve the complete
    # page when the selected output orientation differs from the source. The
    # product image itself still uses cover inside its slot, so it fills the
    # frame without being stretched.
    template_page = fit_contain(template, output_size) if mode in {"template_replace", "cutout"} else fit_cover(template, output_size)
    native_slot = _template_replace_slot(template)
    mapped_slot = _slot_for_contain(native_slot, output_size) if native_slot is not None else None
    if mode == "template_replace":
        # Detect on the native source first, then transform the mask together
        # with the page. This keeps detection stable at both 405x720 and
        # 1080x1920/720x1280 output sizes instead of re-detecting a resized,
        # compressed-looking edge.
        result = _remove_template_product(template_page, detected=mapped_slot)
    else:
        if product_path is None:
            raise ValueError("该合成方式需要商品图")
        product = _open_rgb(product_path)
        background = fit_cover(product, output_size)
        if mode == "auto":
            # A native alpha channel is an explicit instruction from the template
            # designer: transparent pixels are the replacement for a background
            # slot, not a green/white panel to be detected heuristically.
            if _has_transparency(template_page):
                selected = "overlay"
            elif mapped_slot is not None or _product_slot(template_page) is not None:
                selected = "cutout"
            else:
                selected = "overlay" if _looks_like_flat_template(template) else "cutout"
        else:
            selected = mode
        if selected == "overlay":
            result = _apply_overlay(background, template, preserve_logo_pill=preserve_logo_pill, outline_strength=outline_strength)
        else:
            result = _place_cutout(template_page, product=product, detected=mapped_slot)
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
