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
MAX_INPUT_PIXELS = 24_000_000


def _validate_input_image(image: Image.Image) -> None:
    """Reject decompression-bomb-sized inputs before forcing pixel decoding."""
    pixels = int(image.width) * int(image.height)
    if pixels > MAX_INPUT_PIXELS:
        raise ValueError("输入图片像素数不能超过 2400 万")


def _open_rgb(path: str | Path) -> Image.Image:
    with Image.open(path) as image:
        _validate_input_image(image)
        return image.convert("RGB")


def _open_template(path: str | Path) -> Image.Image:
    """Open a template without flattening its native PNG transparency."""
    with Image.open(path) as image:
        _validate_input_image(image)
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


def _wand_background_alpha(image: Image.Image) -> Image.Image | None:
    """Fallback page key for flat templates whose border is not one color.

    Some campaign pages place a photo panel or a soft gradient against the
    canvas edge, so the global border-median key refuses to run and the whole
    template would cover the product photo.  Region-growing from the
    page-colored part of the border (a magic wand with a small per-step
    tolerance) still removes such pages: the fill creeps along gradients but
    stops at hard artwork edges.  A second stage handles a flat frame ring
    around the page: when the first fill removes almost nothing, the uniform
    band directly inside it is treated as the real page color.

    Photographic templates are rejected by a colour-tightness gate: a removed
    campaign page stays close to its border colour, while an eaten photo
    backdrop spans a wide vignette range.  Those templates keep the previous
    fully-opaque overlay behavior.
    """
    if cv2 is None:
        return None
    # Templates that carry a product frame or panel belong to the cutout
    # feature; overlay must not strip their page behind the product photo.
    if _template_replace_slot(image) is not None:
        return None
    rgb = np.array(image.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    median = np.median(border, axis=0)
    near = np.max(np.abs(border.astype(np.float32) - median), axis=1) <= 24
    if float(near.mean()) < 0.10:
        return None

    seeds: list[tuple[int, int]] = []

    def pick(values: np.ndarray, make) -> None:
        idx = np.where(values)[0]
        if idx.size == 0:
            return
        step = max(1, idx.size // 12)
        seeds.extend(make(int(value)) for value in idx[::step])

    pick(near[:width], lambda x: (x, 0))
    pick(near[width:2 * width], lambda x: (x, height - 1))
    pick(near[2 * width:2 * width + height], lambda y: (0, y))
    pick(near[2 * width + height:], lambda y: (width - 1, y))
    mask = np.zeros((height + 2, width + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
    lo = (10, 10, 10)

    def grow(seed_points) -> None:
        for sx, sy in seed_points:
            if mask[sy + 1, sx + 1]:
                continue
            cv2.floodFill(rgb, mask, (sx, sy), 0, lo, lo, flags)

    grow(seeds)
    flooded = mask[1:-1, 1:-1] == 255
    clusters = [median]
    if float(flooded.mean()) < 0.25:
        band = (cv2.dilate(flooded.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & ~flooded
        colors = rgb[band].astype(np.float32)
        if colors.shape[0] > 0:
            inner = np.median(colors, axis=0)
            flat = np.max(np.abs(colors - inner), axis=1) <= 12
            # A frame ring meets the page along one long uniform interface;
            # photographic edges produce a scattered mix instead.
            if float(flat.mean()) >= 0.5:
                clusters.append(inner)
                ys, xs = np.where(band & (np.max(np.abs(rgb.astype(np.float32) - inner), axis=2) <= 12))
                if ys.size:
                    step = max(1, ys.size // 24)
                    grow(list(zip(xs[::step].tolist(), ys[::step].tolist())))
                flooded = mask[1:-1, 1:-1] == 255
    removed = float(flooded.mean())
    if removed < 0.25 or removed > 0.985:
        return None
    distance = np.min(
        np.stack([np.max(np.abs(rgb.astype(np.float32) - color), axis=2) for color in clusters]),
        axis=0,
    )
    tight = distance[flooded]
    if float(tight.mean()) > 45.0 or float(np.percentile(tight, 90)) > 80.0:
        return None

    # Anti-alias ramp against the *local* page color so glyph edges keep a
    # smooth transition instead of a hard 1 px fringe of page color.
    flooded_f = flooded.astype(np.float32)
    count = cv2.boxFilter(flooded_f, -1, (5, 5), normalize=False)
    summed = cv2.boxFilter(rgb.astype(np.float32) * flooded_f[..., None], -1, (5, 5), normalize=False)
    page_est = np.where(count[..., None] > 0, summed / np.maximum(count, 1e-6)[..., None], median)
    edge_distance = np.max(np.abs(rgb.astype(np.float32) - page_est), axis=2)
    ring = cv2.dilate(flooded.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    alpha = np.where(flooded, 0.0, 1.0)
    alpha = np.where(ring & ~flooded, np.clip((edge_distance - 10.0) / 38.0, 0.0, 1.0), alpha)
    # Frame-ring pages can leave tiny page-colored chips wedged in the canvas
    # corners. Drop isolated specks that hug a corner; real artwork sits away
    # from the exact corner or is large enough to survive the area gate.
    opaque = (alpha > 0.5).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(opaque, 8)
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area >= max(64.0, width * height * 0.001):
            continue
        touches = (x <= 1) + (y <= 1) + (x + w >= width - 1) + (y + h >= height - 1)
        if touches >= 2:
            alpha[labels == index] = 0.0
    return Image.fromarray(np.uint8(alpha * 255)).filter(ImageFilter.GaussianBlur(0.3))


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


def _with_alpha(page: Image.Image, alpha: Image.Image) -> Image.Image:
    """Combine a template page with an alpha mask as RGBA."""
    result = page.convert("RGBA")
    result.putalpha(alpha)
    return result


def _unmultiply_page_fringe(page: Image.Image, alpha: Image.Image) -> Image.Image:
    """Recover true edge colors and coverage from page-blended fringe pixels.

    The page key derives alpha from the distance to the page color with a
    narrow ramp, so an anti-aliased glyph edge keeps opaque pixels whose RGB
    is still mostly the white page; composited over a dark product photo they
    read as a 1 px bright hairline around every glyph and counter. For pixels
    near a transparency boundary we re-estimate coverage against the local
    ink color (``observed = c*ink + (1-c)*page``) and unmix the stored RGB,
    which restores both the soft coverage and the real edge color. Interior
    artwork pixels are untouched because only boundary-adjacent pixels are
    re-estimated.
    """
    if cv2 is None:
        return _with_alpha(page, alpha)
    rgb = np.asarray(page.convert("RGB"), dtype=np.float32)
    a = np.asarray(alpha, dtype=np.uint8)
    transparent = a <= 8
    if not transparent.any():
        return _with_alpha(page, alpha)
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    page_color = np.median(border, axis=0)
    # A pixel qualifies as fringe when a transparent pixel is within 2 px;
    # that ring is exactly where the key ramp misjudges coverage.
    near = cv2.dilate(transparent.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    # Both the semi-transparent ramp pixels and the first opaque row inside a
    # glyph edge still carry page-blended color; re-estimate the whole ring.
    fringe = near & (a > 8)
    if not fringe.any():
        return _with_alpha(page, alpha)
    solid = (a >= 250) & ~near
    if not solid.any():
        return _with_alpha(page, alpha)
    _, labels = cv2.distanceTransformWithLabels(
        (~solid).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    # Labels number the nearest solid pixel 1..N in raster order, not by flat
    # index, so map them back through the solid pixel color list.
    solid_colors = rgb.reshape(-1, 3)[solid.ravel()]
    ink = solid_colors[np.clip(labels.ravel(), 1, solid_colors.shape[0]) - 1].reshape(rgb.shape)
    ink_distance = np.max(np.abs(rgb - ink), axis=2)
    page_distance = np.max(np.abs(rgb - page_color), axis=2)
    # observed = c*ink + (1-c)*page, so c is the page distance share of the
    # full ink-to-page distance (page_distance + ink_distance).
    full = page_distance + ink_distance
    coverage = np.clip(page_distance / np.maximum(full, 24.0), 0.0, 1.0)
    # Only trust the re-estimate where the pixel really lies on the page-to-ink
    # segment; JPEG ringing or a second ink colour nearby fails this test and
    # keeps its original alpha.
    on_segment = full >= 60.0
    recovered = (rgb - (1.0 - coverage[..., None]) * page_color) / np.maximum(coverage[..., None], 0.06)
    soften = fringe & on_segment
    out_rgb = rgb.copy()
    out_rgb[soften] = np.clip(recovered[soften], 0.0, 255.0)
    out_a = a.astype(np.float32)
    out_a[soften] = np.minimum(out_a[soften], coverage[soften] * 255.0)
    result = Image.fromarray(np.uint8(out_rgb), "RGB")
    result = result.convert("RGBA")
    result.putalpha(Image.fromarray(np.uint8(out_a), "L"))
    return result


def _glyph_counter_alpha(page: Image.Image, alpha: Image.Image) -> Image.Image:
    """Punch page-white counters of solid dark glyphs through to transparent.

    A flattened JPG template bakes its page color into every enclosed hole, so
    the counter of a headline digit (the inside of a "0") survives the
    border-connected page key as an opaque white blob floating over the
    product photo. Enclosed white fills that belong to the artwork (cartoon
    eyes, copy cards) sit inside thin outline components, while a digit
    counter sits inside a solid glyph body; gating on the dark component's
    fill ratio separates the two without touching the artwork.
    """
    if cv2 is None:
        return alpha
    rgb = np.asarray(page.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    dark = gray <= 100
    original_alpha = np.asarray(alpha, dtype=np.uint8)
    alpha_array = original_alpha.copy()
    height, width = alpha_array.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark.astype(np.uint8), 8)
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        # A dark component touching the canvas border is the page itself on a
        # black template; its enclosed whites are artwork, not counters.
        if x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1:
            continue
        # Solid headline glyphs fill most of their box; outlines, offset
        # shadows and thin rules do not, and their enclosed whites are
        # deliberate artwork fills. CJK glyphs are sparser than latin digits,
        # so the gate stays low enough to include them.
        if area < 500 or area < 0.25 * w * h:
            continue
        component = labels[y:y + h, x:x + w] == index
        # Glyphs printed on an opaque white card or pill keep their counters
        # (the card shows through them); only glyphs sitting directly on the
        # keyed page have page-colored see-through counters. Sample the ring
        # on the true outside of the glyph (border-connected area), ignoring
        # inner holes such as white inlines.
        inv = (~component).astype(np.uint8)
        outside_count, outside_labels, outside_stats, _ = cv2.connectedComponentsWithStats(inv, 4)
        touch = np.unique(np.concatenate((
            outside_labels[0, :], outside_labels[-1, :],
            outside_labels[:, 0], outside_labels[:, -1],
        )))
        outside = np.isin(outside_labels, touch[touch != 0])
        ring = (cv2.dilate(component.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0) & outside
        ring_alpha = original_alpha[y:y + h, x:x + w][ring]
        if ring_alpha.size == 0:
            continue
        ring_rgb = rgb[y:y + h, x:x + w][ring]
        trans = ring_alpha <= 8
        local_page = np.median(ring_rgb[trans], axis=0) if trans.any() else np.median(ring_rgb, axis=0)
        # Glyphs printed on an opaque white card or pill keep their counters:
        # there the ring's opaque pixels share the counter color, while on a
        # keyed page the opaque ring pixels are neighbour glyph inks.
        opaque_ring = ring_alpha > 128
        if opaque_ring.any():
            same = np.max(np.abs(ring_rgb[opaque_ring].astype(np.float32) - local_page), axis=1) <= 32.0
            if float(same.mean()) >= 0.5:
                continue
        hole_count, hole_labels, hole_stats, _ = cv2.connectedComponentsWithStats(inv, 4)
        for j in range(1, hole_count):
            hx, hy, hw, hh, hole_area = hole_stats[j]
            # The outside background touches the component box; a real counter
            # is strictly interior and large enough to read as a hole.
            if hx <= 0 or hy <= 0 or hx + hw >= w or hy + hh >= h:
                continue
            if hole_area < max(300, 0.02 * w * h):
                continue
            hole = hole_labels[hy:hy + hh, hx:hx + hw] == j
            hole_rgb = rgb[y + hy:y + hy + hh, x + hx:x + hx + hw].astype(np.float32)
            like = np.max(np.abs(hole_rgb - local_page), axis=2) <= 32.0
            # Small counters carry a relatively wide anti-aliased border, so
            # the page-color share threshold stays permissive.
            if float(like[hole].mean()) < 0.80:
                continue
            # A hole wrapping a detached dark island (cartoon eye white around
            # its pupil) is artwork fill, not a glyph counter. JPEG/anti-alias
            # specks form thin chains along the hole border, so only compact
            # blob-like dark components count as islands.
            hole_dark = dark[y + hy:y + hy + hh, x + hx:x + hx + hw][hole]
            if hole_dark.any():
                dn, _, dstats, _ = cv2.connectedComponentsWithStats(
                    hole_dark.astype(np.uint8), 8,
                )
                island = False
                for k in range(1, dn):
                    _, _, dw, dh, darea = dstats[k]
                    if darea >= max(120, 0.08 * hole_area) and darea >= 0.45 * dw * dh:
                        island = True
                        break
                if island:
                    continue
            alpha_array[y + hy:y + hy + hh, x + hx:x + hx + hw][hole] = 0
    return Image.fromarray(alpha_array, "L")


def _top_left_logo_bbox(template: Image.Image, alpha: Image.Image) -> tuple[int, int, int, int] | None:
    """Find the compact merchant logo row for a readable backing pill.

    The logo lockup is the first horizontal content band below the top edge;
    the campaign headline lives in a later band separated by an empty gap.
    The previous fixed 10%-height window clipped the headline ascenders and
    unioned them into the logo box, so the pill grew into a slab that covered
    the headline.  Splitting the top area into content bands and stopping at
    the first real gap keeps the pill on the lockup only, and the pill bottom
    is additionally clamped so it can never reach the next band.
    """
    a = np.asarray(alpha, dtype=np.uint8)
    height, width = a.shape
    mask = a > 150
    scan_h = int(height * 0.30)
    scan_w = int(width * 0.78)
    occupancy = mask[:scan_h, :scan_w].sum(axis=1)
    rows = np.where(occupancy > max(2, scan_w * 0.002))[0]
    if rows.size == 0:
        return None
    split_gap = max(4, round(height * 0.004))
    bands: list[tuple[int, int]] = []
    start = prev = int(rows[0])
    for y in rows[1:]:
        y = int(y)
        if y - prev > split_gap:
            bands.append((start, prev))
            start = y
        prev = y
    bands.append((start, prev))
    y0, y1 = bands[0]
    next_top: int | None = None
    for band_start, band_end in bands[1:]:
        # A tagline sitting directly under the mark belongs to the same
        # lockup; the headline is separated by a clearly larger gap.
        if band_start - y1 <= max(6, round(height * 0.006)) and band_end - y0 <= height * 0.12:
            y1 = band_end
        else:
            next_top = band_start
            break
    cols = np.where(mask[y0:y1 + 1, :scan_w].any(axis=0))[0]
    if cols.size == 0:
        return None
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    # Headlines and full-width banners are not logos: they are tall or span
    # most of the canvas, while merchant lockups stay compact in the corner.
    if (y1 - y0) > height * 0.10 or (x1 - x0) > width * 0.62:
        return None
    if (y1 - y0) < max(6, round(height * 0.004)) or (x1 - x0) < max(8, round(width * 0.02)):
        return None
    pad_x = max(12, round(width * 0.018))
    pad_y = max(10, round(height * 0.010))
    bottom = y1 + pad_y
    if next_top is not None:
        bottom = min(bottom, next_top - max(2, round(height * 0.002)))
    bottom = max(bottom, y1 + max(4, round(height * 0.004)))
    return max(0, x0 - pad_x), max(0, y0 - pad_y), min(width, x1 + pad_x), min(height, bottom)


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
            alpha = _wand_background_alpha(page)
        if alpha is None:
            alpha = _soft_white_alpha(page)
        alpha = _glyph_counter_alpha(page, alpha)

    # Native-alpha templates already contain the designer's exact pill and
    # edge treatment. Do not enlarge or redraw either of them.
    if preserve_logo_pill and not uses_native_alpha:
        bbox = _top_left_logo_bbox(page, alpha)
        if bbox is not None:
            x0, y0, x1, y1 = bbox
            box_alpha = np.asarray(alpha, dtype=np.uint8)[y0:y1, x0:x1]
            owned = float((box_alpha > 128).mean()) >= 0.8
            # A template that ships its own badge already fills the lockup
            # box; a second pill behind it would show as a larger slab.
            if not owned:
                opaque = box_alpha > 128
                rgb_box = np.asarray(page.convert("RGB"), dtype=np.float32)[y0:y1, x0:x1]
                luma = 0.299 * rgb_box[..., 0] + 0.587 * rgb_box[..., 1] + 0.114 * rgb_box[..., 2]
                logo_luma = float(np.median(luma[opaque])) if opaque.any() else 0.0
                # A white pill would swallow an all-white lockup; use a dark
                # one only for that case so the mark keeps its contrast.
                fill = (24, 26, 32, 236) if logo_luma > 200 else (255, 255, 255, 248)
                pill = Image.new("RGBA", size, (255, 255, 255, 0))
                radius = max(12, round(min(size) * 0.025))
                radius = min(radius, max(6, (y1 - y0) // 2))
                ImageDraw.Draw(pill).rounded_rectangle(bbox, radius=radius, fill=fill)
                background = Image.alpha_composite(background.convert("RGBA"), pill)

    if uses_native_alpha:
        graphic = _with_alpha(page, alpha)
    else:
        graphic = _unmultiply_page_fringe(page, alpha)

    # A restrained white keyline keeps orange promotional type legible over
    # food. Solid black type already carries its own contrast, so the keyline
    # is masked away next to dark artwork: a white ring hugging a black
    # headline reads as a cutout artifact instead of a design stroke.
    if outline_strength > 0.01 and not uses_native_alpha:
        expanded = alpha.filter(ImageFilter.MaxFilter(9))
        border = ImageChops.subtract(expanded, alpha)
        border = border.point(lambda value: round(value * max(0.0, min(1.0, outline_strength))))
        # The expansion also creeps into enclosed holes (glyph counters), so
        # keep the keyline only on the true outside of the artwork.
        exterior = _connected_to_border(np.asarray(alpha, dtype=np.uint8) <= 8)
        border = ImageChops.multiply(border, Image.fromarray(np.uint8(exterior) * 255, "L"))
        rgb_array = np.asarray(page.convert("RGB"), dtype=np.float32)
        luma = 0.299 * rgb_array[..., 0] + 0.587 * rgb_array[..., 1] + 0.114 * rgb_array[..., 2]
        alpha_array = np.asarray(alpha, dtype=np.uint8)
        # Only dark artwork counts: a black template page must not suppress
        # the keyline everywhere, and keyed-out page pixels are not content.
        dark = Image.fromarray(np.uint8((luma <= 100) & (alpha_array >= 128)) * 255, "L")
        # The keyline ring lies just outside the glyph edge, where the page
        # itself is light; test the dilated dark mask so a ring hugging black
        # type is recognised as belonging to dark artwork and dropped.
        keep = ImageChops.invert(dark.filter(ImageFilter.MaxFilter(17)))
        border = ImageChops.multiply(border, keep)
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
            # A white copy card (bright page color plus dark text strokes and
            # almost no saturation) is typography, not a product placeholder.
            # Without this test a headline speech bubble becomes a slot and
            # auto mode routes a pure overlay template into cutout.
            hsv = cv2.cvtColor(rgb[y:y + box_height, x:x + box_width], cv2.COLOR_RGB2HSV)
            bright_ratio = float(np.mean((hsv[:, :, 2] >= 205) & (hsv[:, :, 1] <= 42)))
            chroma_ratio = float(np.mean((hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 40)))
            dark_ratio = float(np.mean(hsv[:, :, 2] <= 110))
            if bright_ratio > 0.55 and chroma_ratio < 0.12 and 0.02 < dark_ratio < 0.40:
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


def _manual_slot(
    template: Image.Image,
    slot: tuple[float, float, float, float],
) -> tuple[tuple[int, int, int, int], Image.Image]:
    """Build a native-space slot from user-drawn normalized canvas ratios."""
    width, height = template.size
    ratio_x, ratio_y, ratio_w, ratio_h = slot
    x0 = max(0, min(width, int(round(ratio_x * width))))
    y0 = max(0, min(height, int(round(ratio_y * height))))
    x1 = max(0, min(width, int(round((ratio_x + ratio_w) * width))))
    y1 = max(0, min(height, int(round((ratio_y + ratio_h) * height))))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ValueError("手动框选区域过小，请重新框选")
    mask = Image.new("L", template.size, 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return (x0, y0, x1, y1), mask


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


def _expand_dark_frame_edges(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Include a dark outer frame when a line detector lands on its inner edge.

    JPEG posters frequently have a 2–5 px black rule above a white photo panel.
    Hough lines tend to select the first high-contrast transition *inside* that
    rule, leaving the rule visible in the generated green template.  Expand
    only over rows/columns that are consistently dark across the panel; this
    avoids eating into a photographic or pastel background around the slot.
    """
    height, width = rgb.shape[:2]
    x0, y0, x1, y1 = bbox
    max_pad = max(2, round(min(width, height) * 0.025))
    margin_x = max(1, round((x1 - x0) * 0.04))
    margin_y = max(1, round((y1 - y0) * 0.04))

    def dark_ratio(values: np.ndarray) -> float:
        if values.size == 0:
            return 0.0
        # Dark navy/black rules remain dark in all channels after JPEG
        # compression; use max() so a coloured background is not mistaken for
        # a frame merely because one channel is low.
        return float(np.mean(np.max(values, axis=-1) <= 118))

    for _ in range(max_pad):
        if y0 <= 0:
            break
        sample = rgb[y0 - 1, x0 + margin_x : max(x0 + margin_x + 1, x1 - margin_x)]
        if dark_ratio(sample) < 0.55:
            break
        y0 -= 1
    for _ in range(max_pad):
        if y1 >= height:
            break
        sample = rgb[y1, x0 + margin_x : max(x0 + margin_x + 1, x1 - margin_x)]
        if dark_ratio(sample) < 0.55:
            break
        y1 += 1
    for _ in range(max_pad):
        if x0 <= 0:
            break
        sample = rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), x0 - 1]
        if dark_ratio(sample) < 0.55:
            break
        x0 -= 1
    for _ in range(max_pad):
        if x1 >= width:
            break
        sample = rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), x1]
        if dark_ratio(sample) < 0.55:
            break
        x1 += 1
    return x0, y0, x1, y1


def _expand_high_contrast_edges(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Recover a blended photo edge from a narrow, consistent colour jump."""
    height, width = rgb.shape[:2]
    x0, y0, x1, y1 = bbox
    max_pad = max(2, round(min(width, height) * 0.018))
    margin_x = max(2, round((x1 - x0) * 0.035))
    margin_y = max(2, round((y1 - y0) * 0.035))

    def jump(first: np.ndarray, second: np.ndarray) -> float:
        if first.size == 0 or second.size == 0:
            return 0.0
        return float(np.mean(np.abs(first.astype(np.float32) - second.astype(np.float32))))

    for candidate in range(x0 - 1, max(-1, x0 - max_pad - 1), -1):
        if candidate <= 0:
            break
        if jump(rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), candidate], rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), candidate - 1]) >= 22.0:
            x0 = candidate
            break
    for candidate in range(x1, min(width, x1 + max_pad)):
        if candidate >= width:
            break
        if jump(rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), candidate], rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), candidate - 1]) >= 22.0:
            x1 = candidate + 1
            break
    return x0, y0, x1, y1


def _expand_light_frame_edges(
    rgb: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Include a contiguous near-white card edge around a photo slot."""
    height, width = rgb.shape[:2]
    x0, y0, x1, y1 = bbox
    max_pad = max(2, round(min(width, height) * 0.04))
    margin_x = max(2, round((x1 - x0) * 0.035))
    margin_y = max(2, round((y1 - y0) * 0.035))

    def light_ratio(values: np.ndarray) -> float:
        if values.size == 0:
            return 0.0
        hsv = cv2.cvtColor(values.reshape(1, -1, 3), cv2.COLOR_RGB2HSV)[0]
        return float(np.mean((hsv[:, 2] >= 205) & (hsv[:, 1] <= 42)))

    for _ in range(max_pad):
        if y0 <= 0:
            break
        sample = rgb[y0 - 1, x0 + margin_x : max(x0 + margin_x + 1, x1 - margin_x)]
        if light_ratio(sample) < 0.65:
            break
        y0 -= 1
    for _ in range(max_pad):
        if y1 >= height:
            break
        sample = rgb[y1, x0 + margin_x : max(x0 + margin_x + 1, x1 - margin_x)]
        if light_ratio(sample) < 0.65:
            break
        y1 += 1
    for _ in range(max_pad):
        if x0 <= 0:
            break
        sample = rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), x0 - 1]
        if light_ratio(sample) < 0.65:
            break
        x0 -= 1
    for _ in range(max_pad):
        if x1 >= width:
            break
        sample = rgb[y0 + margin_y : max(y0 + margin_y + 1, y1 - margin_y), x1]
        if light_ratio(sample) < 0.65:
            break
        x1 += 1
    return x0, y0, x1, y1


def _line_framed_photo_slot(
    background: Image.Image,
    *,
    include_frame: bool = False,
    _preview: bool = True,
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Find a soft rectangular photo panel when the source has no frame line.

    Downloaded campaign JPGs often have a product photo blended directly into
    the surrounding artwork.  The four panel edges are still visible as long
    low-contrast lines, so a multi-threshold Hough pass is a safer fallback
    than treating the whole template as a product image.
    """
    if cv2 is None:
        return None
    # Hough detection is quadratic in the number of horizontal/vertical line
    # candidates.  Large phone posters can contain thousands of texture edges,
    # so detect on a bounded preview and map the chosen rectangle back to the
    # native canvas.  This keeps recognition stable for 1080×1920 and 1320×2362
    # inputs without sacrificing the original output resolution.
    if _preview and max(background.size) > 900:
        preview_scale = 900.0 / max(background.size)
        preview_size = (
            max(64, round(background.width * preview_scale)),
            max(64, round(background.height * preview_scale)),
        )
        preview = background.resize(preview_size, Image.Resampling.BILINEAR)
        detected = _line_framed_photo_slot(preview, include_frame=include_frame, _preview=False)
        if detected is None:
            return None
        (px0, py0, px1, py1), _ = detected
        x0 = max(0, min(background.width - 1, int(np.floor(px0 / preview_scale))))
        y0 = max(0, min(background.height - 1, int(np.floor(py0 / preview_scale))))
        x1 = max(x0 + 1, min(background.width, int(np.ceil(px1 / preview_scale))))
        y1 = max(y0 + 1, min(background.height, int(np.ceil(py1 / preview_scale))))
        mask = Image.new("L", background.size, 0)
        ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
        return (x0, y0, x1, y1), mask
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
    envelope_used = False
    if include_frame:
        # Some photo cards have a strong top/bottom rule but only one visible
        # vertical edge (the other edge blends into the background).  Recover
        # the full panel from the shared horizontal span when it is materially
        # wider than the four-line candidate selected above.  This is common
        # in lifestyle templates where the photo fades into a warm backdrop.
        envelope_candidates: list[tuple[float, tuple[int, int, int, int]]] = []
        for top in horizontal:
            for bottom in horizontal:
                ey0, ey1 = top[0], bottom[0]
                if (
                    ey1 - ey0 < height * 0.30
                    or ey1 - ey0 > height * 0.82
                    or ey0 < height * 0.12
                    or ey1 > height * 0.98
                ):
                    continue
                ex0, ex1 = max(top[1], bottom[1]), min(top[2], bottom[2])
                if ex1 - ex0 < width * 0.35 or ex0 < width * 0.02 or ex1 > width * 0.98:
                    continue
                span = max(1, ex1 - ex0)
                top_coverage = coverage(top[1], top[2], ex0, ex1)
                bottom_coverage = coverage(bottom[1], bottom[2], ex0, ex1)
                score = span * (ey1 - ey0) * (min(top_coverage, bottom_coverage) ** 2)
                envelope_candidates.append((score, (ex0, ey0, ex1, ey1)))
        if envelope_candidates:
            envelope_score, envelope = max(envelope_candidates, key=lambda item: item[0])
            # On tall posters a long decorative rule above the photo can make
            # a too-tall envelope win on area alone.  If a later, well-formed
            # panel is nearly as strong, prefer it; this keeps the headline
            # and the empty gap above the actual product image intact.
            if height > width:
                lower_candidates = [
                    item for item in envelope_candidates
                    if item[1][1] >= height * 0.36
                ]
                if lower_candidates:
                    lower_score, lower_envelope = max(lower_candidates, key=lambda item: item[0])
                    if lower_score >= envelope_score * 0.72:
                        envelope_score, envelope = lower_score, lower_envelope
            envelope_area = (envelope[2] - envelope[0]) * (envelope[3] - envelope[1])
            selected_area = max(1, (x1 - x0) * (y1 - y0))
            if envelope_area >= selected_area * 1.12 and (envelope[2] - envelope[0]) >= (x1 - x0) * 1.06:
                x0, y0, x1, y1 = envelope
                envelope_used = True
        # A real product card may have no distinct bottom rule: the photo
        # fades into the surrounding artwork while its two vertical edges
        # continue a little farther down.  Use those paired edges as a
        # conservative extension signal so an internal label baseline cannot
        # truncate the replacement area.
        side_tolerance = max(5, round(width * 0.015))

        def vertical_end_near(target_x: int) -> int | None:
            spans = [
                line[2]
                for line in vertical
                if abs(line[0] - target_x) <= side_tolerance
                and line[1] <= y0 + max(4, round((y1 - y0) * 0.03))
                and line[2] >= y0 + round((y1 - y0) * 0.55)
            ]
            return max(spans) if spans else None

        left_end, right_end = vertical_end_near(x0), vertical_end_near(x1)
        if left_end is not None and right_end is not None:
            common_end = min(left_end, right_end)
            extension = rgb[y1 : min(height, common_end + 1), x0:x1].astype(np.float32)
            # Ignore long vertical edges caused by a textured background.  A
            # genuine continuation of a product photo has materially more
            # local colour/texture variation than the flat carpet or paper
            # outside it; this guards the c0 template from a 27 px overrun.
            texture = float(np.mean(np.std(extension, axis=2))) if extension.size else 0.0
            gradient = 0.0
            if extension.shape[0] > 1 and extension.shape[1] > 1:
                gradient = float(
                    np.mean(np.abs(np.diff(extension, axis=0)))
                    + np.mean(np.abs(np.diff(extension, axis=1)))
                )
            if (
                common_end > y1 + max(4, round(height * 0.01))
                and (texture >= 32.0 or gradient >= 20.0)
            ):
                y1 = min(height, common_end + max(2, round(height * 0.008)))
    if include_frame:
        x0, y0, x1, y1 = _expand_dark_frame_edges(rgb, (x0, y0, x1, y1))
        if envelope_used:
            x0, y0, x1, y1 = _expand_high_contrast_edges(rgb, (x0, y0, x1, y1))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1, y1), fill=255)
    return (x0, y0, x1, y1), mask


def _slot_is_text_card(rgb: np.ndarray, bbox: tuple[int, int, int, int]) -> bool:
    """Whether a candidate slot region is really a typography card.

    Fallback slot detectors key on row/column transitions, so a white speech
    bubble with dark copy produces the same profile as a soft photo card.  A
    product placeholder holds a photograph: saturated or dark pixels across
    the panel.  A copy card is mostly page-bright with a share of text strokes
    and almost no saturation.
    """
    x0, y0, x1, y1 = bbox
    crop = rgb[y0:y1, x0:x1]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    bright_ratio = float(np.mean((hsv[:, :, 2] >= 205) & (hsv[:, :, 1] <= 42)))
    chroma_ratio = float(np.mean((hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 40)))
    dark_ratio = float(np.mean(hsv[:, :, 2] <= 110))
    return bright_ratio > 0.55 and chroma_ratio < 0.12 and 0.02 < dark_ratio < 0.40


def _profile_photo_slot(background: Image.Image) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Recover a low-contrast photo card from long row/column transitions."""
    if cv2 is None:
        return None
    source_width, source_height = background.size
    scale = min(1.0, 900.0 / max(source_width, source_height))
    if scale < 1.0:
        preview = background.resize(
            (max(64, round(source_width * scale)), max(64, round(source_height * scale))),
            Image.Resampling.BILINEAR,
        )
    else:
        preview = background
    rgb = np.asarray(preview.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    smooth = cv2.GaussianBlur(gray, (0, 0), max(1.4, min(width, height) * 0.0035))
    column_profile = np.mean(np.abs(np.diff(smooth, axis=1)), axis=0)
    row_profile = np.mean(np.abs(np.diff(smooth, axis=0)), axis=1)
    profile_window = max(3, round(min(width, height) * 0.006))
    kernel = np.ones(profile_window, dtype=np.float32) / profile_window
    column_profile = np.convolve(column_profile, kernel, mode="same")
    row_profile = np.convolve(row_profile, kernel, mode="same")

    def peaks(profile: np.ndarray, start: int, stop: int, limit: int) -> list[int]:
        local = cv2.dilate(profile.astype(np.float32), np.ones((1, profile_window * 2 + 1), np.float32)).ravel()
        candidates = np.where((profile >= local - 1e-6) & (profile > 0))[0]
        candidates = candidates[(candidates >= start) & (candidates < stop)]
        candidates = candidates[np.argsort(profile[candidates])[::-1]]
        selected: list[int] = []
        for candidate in candidates:
            position = int(candidate)
            if all(abs(position - previous) >= profile_window for previous in selected):
                selected.append(position)
            if len(selected) >= limit:
                break
        return selected

    row_peaks = peaks(row_profile, round(height * 0.36), round(height * 0.98), 56)
    column_peaks = peaks(column_profile, round(width * 0.03), round(width * 0.97), 72)
    if len(row_peaks) < 2 or len(column_peaks) < 2:
        return None
    row_baseline = max(0.45, float(np.median(row_profile)))
    column_baseline = max(0.45, float(np.median(column_profile)))
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for top in row_peaks:
        for bottom in row_peaks:
            if bottom <= top or bottom - top < height * 0.24 or bottom - top > height * 0.78:
                continue
            row_strength = min(float(row_profile[top]), float(row_profile[bottom]))
            if row_strength < max(row_baseline * 1.35, 1.7):
                continue
            for left in column_peaks:
                for right in column_peaks:
                    if right <= left or right - left < width * 0.40 or right - left > width * 0.95:
                        continue
                    if left < width * 0.02 or right > width * 0.98:
                        continue
                    column_strength = min(float(column_profile[left]), float(column_profile[right]))
                    if column_strength < max(column_baseline * 1.18, 1.25):
                        continue
                    inner = gray[top + 5 : bottom - 5, left + 5 : right - 5]
                    texture = float(np.std(inner)) if inner.size else 0.0
                    if texture < 8.0:
                        continue
                    area = (right - left) * (bottom - top)
                    score = area * (row_strength / row_baseline) ** 0.8 * (column_strength / column_baseline) ** 0.8
                    score *= 1.0 + min(texture, 80.0) / 80.0
                    candidates.append((score, (left, top, right, bottom)))
    if not candidates:
        return None
    _, (left, top, right, bottom) = max(candidates, key=lambda item: item[0])
    x0 = max(0, min(source_width - 1, int(np.floor(left / scale))))
    y0 = max(0, min(source_height - 1, int(np.floor(top / scale))))
    x1 = max(x0 + 1, min(source_width, int(np.ceil(right / scale))))
    y1 = max(y0 + 1, min(source_height, int(np.ceil(bottom / scale))))
    if cv2 is not None and _slot_is_text_card(np.asarray(background.convert("RGB"), dtype=np.uint8), (x0, y0, x1, y1)):
        return None
    mask = Image.new("L", background.size, 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return (x0, y0, x1, y1), mask


def _texture_photo_slot(
    background: Image.Image,
    *,
    _preview: bool = True,
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Recover a photo panel whose boundary has no usable straight edge.

    Some campaign templates put a JPEG photo directly on a paper-coloured
    page.  There is no border to find: the reliable signal is that the photo
    has much more local texture than the surrounding flat artwork.  Use row
    and column texture profiles only as a conservative final fallback after
    the green, contour, and Hough detectors have failed.  This keeps headline
    text and footer copy from becoming a replacement slot while covering
    rounded or softly blended photo cards.
    """
    if cv2 is None:
        return None
    if _preview and max(background.size) > 900:
        preview_scale = 900.0 / max(background.size)
        preview_size = (
            max(64, round(background.width * preview_scale)),
            max(64, round(background.height * preview_scale)),
        )
        preview = background.resize(preview_size, Image.Resampling.BILINEAR)
        detected = _texture_photo_slot(preview, _preview=False)
        if detected is None:
            return None
        (px0, py0, px1, py1), _ = detected
        x0 = max(0, min(background.width - 1, int(np.floor(px0 / preview_scale))))
        y0 = max(0, min(background.height - 1, int(np.floor(py0 / preview_scale))))
        x1 = max(x0 + 1, min(background.width, int(np.ceil(px1 / preview_scale))))
        y1 = max(y0 + 1, min(background.height, int(np.ceil(py1 / preview_scale))))
        mask = Image.new("L", background.size, 0)
        ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
        return (x0, y0, x1, y1), mask

    rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    if width < 120 or height < 180:
        return None
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    magnitude = cv2.magnitude(grad_x, grad_y)
    row_profile = np.mean(magnitude, axis=1)
    col_profile = np.mean(magnitude, axis=0)

    def smooth(profile: np.ndarray) -> np.ndarray:
        window = max(7, round(min(width, height) * 0.012))
        if window % 2 == 0:
            window += 1
        kernel = np.ones(window, dtype=np.float32) / float(window)
        return np.convolve(profile, kernel, mode="same")

    row_profile, col_profile = smooth(row_profile), smooth(col_profile)

    def runs(profile: np.ndarray, threshold: float, start: int, end: int) -> list[tuple[int, int, float]]:
        active = (profile >= threshold).astype(np.uint8)
        active[:start] = 0
        active[end:] = 0
        # Close tiny texture dips inside a photo while leaving separate text
        # lines as individual components.
        kernel_size = max(5, round(min(width, height) * 0.012))
        kernel = np.ones((1, kernel_size), np.uint8) if profile is col_profile else np.ones((kernel_size, 1), np.uint8)
        closed = cv2.morphologyEx(active.reshape(1, -1) if profile is col_profile else active.reshape(-1, 1), cv2.MORPH_CLOSE, kernel)
        active = closed.ravel()
        result: list[tuple[int, int, float]] = []
        index = 0
        while index < len(active):
            if not active[index]:
                index += 1
                continue
            finish = index
            while finish < len(active) and active[finish]:
                finish += 1
            result.append((index, finish, float(np.mean(profile[index:finish]))))
            index = finish
        return result

    def threshold(profile: np.ndarray) -> float:
        q25, q75 = np.percentile(profile, (25, 75))
        return max(4.0, float(q25 + 0.12 * (q75 - q25)))

    row_runs = [
        item
        for item in runs(row_profile, threshold(row_profile), round(height * 0.15), round(height * 0.98))
        if height * 0.30 <= item[1] - item[0] <= height * 0.82
    ]
    col_runs = [
        item
        for item in runs(col_profile, threshold(col_profile), round(width * 0.01), round(width * 0.99))
        if width * 0.30 <= item[1] - item[0] <= width * 0.98
    ]
    if not row_runs or not col_runs:
        return None

    # Select the largest coherent lower-page texture block.  Text banners are
    # shorter than the required panel height and therefore cannot win here.
    best: tuple[float, tuple[int, int, int, int]] | None = None
    row_outside = float(np.median(np.concatenate((row_profile[: max(1, round(height * 0.12))], row_profile[round(height * 0.90) :]))))
    col_outside = float(np.median(np.concatenate((col_profile[: max(1, round(width * 0.08))], col_profile[round(width * 0.92) :]))))
    for ry0, ry1, rmean in row_runs:
        row_contrast = rmean / max(1.0, row_outside)
        if row_contrast < 1.35:
            continue
        for cx0, cx1, cmean in col_runs:
            col_contrast = cmean / max(1.0, col_outside)
            if col_contrast < 1.20:
                continue
            area = (rx := cx1 - cx0) * (ry := ry1 - ry0)
            score = area * min(row_contrast, 8.0) * min(col_contrast, 5.0)
            if best is None or score > best[0]:
                best = (score, (cx0, ry0, cx1, ry1))
    if best is None:
        return None
    x0, y0, x1, y1 = best[1]

    # Gradient profiles can stop early in a smooth photo corner.  Refine both
    # axes with colour distance measured against the flat page around the
    # candidate.  On the Meitu sample this recovers the exact x=115..965 and
    # y=933..1700 edges even though only three sides have visible contrast.
    def refine_axis(
        profile: np.ndarray,
        initial_start: int,
        initial_end: int,
        *,
        horizontal: bool,
    ) -> tuple[int, int]:
        outside_start = max(1, round(len(profile) * (0.08 if horizontal else 0.10)))
        outside_end = max(1, round(len(profile) * (0.92 if horizontal else 0.90)))
        left = profile[: max(1, initial_start - outside_start)]
        right = profile[min(len(profile), initial_end + outside_start) :]
        outside = np.concatenate((left, right)) if left.size and right.size else profile
        outside_level = float(np.median(outside)) if outside.size else 0.0
        inside = profile[initial_start:initial_end]
        inside_level = float(np.median(inside)) if inside.size else 0.0
        if inside_level < outside_level * 1.20 + 5.0:
            return initial_start, initial_end
        level = max(8.0, outside_level + (inside_level - outside_level) * 0.12)
        active = (profile >= level).astype(np.uint8)
        kernel_size = max(5, round(len(profile) * 0.012))
        kernel = np.ones((1, kernel_size), np.uint8) if horizontal else np.ones((kernel_size, 1), np.uint8)
        shape = active.reshape(1, -1) if horizontal else active.reshape(-1, 1)
        active = cv2.morphologyEx(shape, cv2.MORPH_CLOSE, kernel).ravel()
        components: list[tuple[int, int]] = []
        index = 0
        while index < len(active):
            if not active[index]:
                index += 1
                continue
            finish = index
            while finish < len(active) and active[finish]:
                finish += 1
            if finish - index >= len(profile) * 0.30:
                components.append((index, finish))
            index = finish
        overlap = [
            component
            for component in components
            if component[0] <= initial_start + (initial_end - initial_start) * 0.45
            and component[1] >= initial_end - (initial_end - initial_start) * 0.45
        ]
        return max(overlap, key=lambda component: component[1] - component[0]) if overlap else (initial_start, initial_end)

    top_strip = rgb[max(0, y0 - max(8, round(height * 0.08))) : y0, x0:x1]
    bottom_strip = rgb[y1 : min(height, y1 + max(8, round(height * 0.08))), x0:x1]
    if top_strip.size and bottom_strip.size:
        background_colour = np.median(np.concatenate((top_strip.reshape(-1, 3), bottom_strip.reshape(-1, 3)), axis=0), axis=0)
        row_colour_profile = np.linalg.norm(np.mean(rgb[:, x0:x1], axis=1) - background_colour, axis=1)
        y0, y1 = refine_axis(row_colour_profile, y0, y1, horizontal=False)

    left_strip = rgb[y0:y1, : max(8, round(width * 0.08))]
    right_strip = rgb[y0:y1, min(width, x1 + max(8, round(width * 0.08))) :]
    if left_strip.size and right_strip.size:
        background_colour = np.median(np.concatenate((left_strip.reshape(-1, 3), right_strip.reshape(-1, 3)), axis=0), axis=0)
        col_colour_profile = np.linalg.norm(np.mean(rgb[y0:y1], axis=0) - background_colour, axis=1)
        x0, x1 = refine_axis(col_colour_profile, x0, x1, horizontal=True)

    if _slot_is_text_card(rgb, (x0, y0, x1, y1)):
        return None
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
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
    panel = active[y0:y1, x0:x1]
    occupancy = float(np.mean(panel))
    if occupancy < 0.78:
        return False
    # A green product package can occupy most of its bounding box while still
    # containing irregular white labels and cut-outs.  A real chroma panel is
    # rectangular: almost every row and column remains active.  The lower
    # quantile rejects the photographed green package from horizontal sample
    # templates without rejecting mildly compressed green placeholders.
    row_fill = np.mean(panel, axis=1)
    col_fill = np.mean(panel, axis=0)
    return bool(
        np.quantile(row_fill, 0.10) >= 0.58
        and np.quantile(col_fill, 0.10) >= 0.58
    )


def _template_replace_slot(
    background: Image.Image,
    *,
    include_frame: bool = False,
) -> tuple[tuple[int, int, int, int], Image.Image] | None:
    """Locate the best product area for the explicit template-replace mode."""
    # A fully green panel is an explicit placeholder and has a more reliable
    # boundary than Hough lines. Prefer it when it fills a rectangular area;
    # isolated green artwork still falls through to structural detection.
    green = _green_slot(background)
    if _is_solid_green_panel(background, green):
        return green
    # Structural frame detection handles photographed panels and dark/white
    # placeholders while preserving their decorative border.
    transparent = _transparent_product_slot(background)
    framed = None if transparent is not None else _framed_photo_slot(background, preserve_frame=not include_frame)
    detected = (
        transparent
        or framed
        or _line_framed_photo_slot(background, include_frame=include_frame)
        or _texture_photo_slot(background)
        or _profile_photo_slot(background)
        or green
        or _product_slot(background)
    )
    # ``_open_template`` normalizes every input to RGBA, including JPGs whose
    # alpha channel is fully opaque.  Apply the light-card expansion to those
    # templates too; gating on the presence of an ``A`` band left the white
    # strip above the 1f107c... product card untouched in the real compose
    # path even though direct RGB detection looked correct.
    alpha_is_opaque = "A" not in background.getbands() or background.getchannel("A").getextrema()[0] >= 250
    if detected is not None and framed is not None and include_frame and alpha_is_opaque:
        bbox, slot_alpha = detected
        if not _mask_is_green(background, slot_alpha):
            rgb = np.asarray(background.convert("RGB"), dtype=np.uint8)
            expanded = _expand_light_frame_edges(rgb, bbox)
            if expanded != bbox:
                x0, y0, x1, y1 = expanded
                mask = Image.new("L", background.size, 0)
                ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
                detected = expanded, mask
    return detected


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


def _enhance_output_detail(
    image: Image.Image,
    source_size: tuple[int, int],
    output_size: tuple[int, int],
) -> Image.Image:
    """Restore a small amount of edge detail after a canvas upscale.

    Mobile campaign templates are often uploaded at 405×720 or 720×405 and
    exported at the fixed 1080×1920/1280×720 presets.  Upscaling alone makes
    Chinese type, thin rules and product edges look soft.  A single restrained
    unsharp pass is applied only when the requested canvas is larger than the
    source; JPEG quality and dimensions remain governed by the existing
    300 KB encoder below.
    """
    scale = max(output_size[0] / max(1, source_size[0]), output_size[1] / max(1, source_size[1]))
    if scale <= 1.02:
        return image
    radius = 0.46 if scale < 2.0 else 0.58
    percent = 108 if scale < 2.0 else 115
    return image.convert("RGB").filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=2))


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
    manual_slot: tuple[float, float, float, float] | None = None,
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
    # All slot-based flows preserve the complete template page when the
    # requested output orientation differs from the source.  ``auto`` can
    # resolve to cutout after it detects a placeholder, so it must use the
    # same contain transform up front; otherwise the native slot mask and the
    # rendered page would be mapped through different crops.  Overlay keeps
    # its documented cover behavior because it intentionally fills the whole
    # canvas with the product scene.
    template_page = fit_contain(template, output_size) if mode in {"template_replace", "cutout", "auto"} else fit_cover(template, output_size)
    # A user-drawn rectangle overrides automatic detection so awkward templates
    # can still be cleaned precisely. It is expressed in native template ratios
    # and mapped through the same contain transform as the page.
    if mode == "template_replace" and manual_slot is not None:
        native_slot = _manual_slot(template, manual_slot)
    else:
        # Template replacement deliberately includes the outer photo frame so a
        # dark rule from the source poster cannot remain above the green panel.
        # Other modes use the default frame-preserving detector for direct
        # cutout/auto composition.
        native_slot = _template_replace_slot(template, include_frame=(mode == "template_replace"))
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
    # Apply the detail pass after compositing so both the preserved template
    # typography and the inserted product benefit from the same final-scale
    # treatment.  It is intentionally skipped for same-size exports.
    result = _enhance_output_detail(result, template.size, output_size)
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
