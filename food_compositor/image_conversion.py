"""Decode real image content and export static JPEG/PNG without poster rules."""
from __future__ import annotations

import io
import re
import shutil
from pathlib import Path
from functools import lru_cache

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from PIL.TiffImagePlugin import IMAGEWIDTH, IMAGELENGTH

from .cancellation import check_cancelled
from .storage_guard import ensure_space

MAX_PIXELS = 60_000_000
RESULT_THUMB_EDGE = 480
ORIENTATION = 274  # TIFF/EXIF tag for display orientation.
UNDO_TIFF_ORIENTATION = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}
READ_FORMATS = {'JPEG', 'PNG', 'WEBP', 'AVIF', 'BMP', 'DIB', 'GIF', 'TIFF',
                'ICO', 'JPEG2000', 'PPM', 'PCX', 'TGA', 'DDS', 'PSD', 'HEIF'}


@lru_cache(maxsize=1)
def capabilities():
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except (ImportError, OSError):
        pass
    Image.init()
    # Some Pillow builds register AVIF but contain no AV1 codec. The plugin
    # bundles its own codec and must register after Pillow's built-in plugins.
    try:
        import pillow_avif  # noqa: F401
    except (ImportError, OSError):
        pass
    available = sorted(READ_FORMATS.intersection(Image.OPEN))
    return {'input_formats': available, 'heif': 'HEIF' in available,
            'max_pixels': MAX_PIXELS, 'output_formats': ['jpeg', 'png']}


def _open(path):
    try:
        return Image.open(path, formats=capabilities()['input_formats'])
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError('无法解码此图片，文件可能已损坏或格式不受支持；HEIC/HEIF 需安装 pillow-heif，不支持 SVG、PDF 和相机 RAW') from exc


def estimate_conversion_memory(path):
    try:
        with _open(path) as image:
            pixels = image.width * image.height
    except (ValueError, Image.DecompressionBombError):
        pixels = 0
    return 256 * 1024**2 + pixels * 24


def _stored_size(image):
    if image.format == 'TIFF':
        return image.tag_v2[IMAGEWIDTH], image.tag_v2[IMAGELENGTH]
    return image.size


def inspect_image(source):
    check_cancelled()
    with _open(source) as image:
        width, height = _stored_size(image)
        if width * height > MAX_PIXELS:
            raise ValueError('格式转换输入图片不能超过 6000 万像素')
        result = {'input_format': image.format, 'width': width, 'height': height,
                  'frames': getattr(image, 'n_frames', 1)}
    check_cancelled()
    return result


def _select_frame(image, frame):
    frames = getattr(image, 'n_frames', 1)
    if frame is None and frames > 1:
        raise ValueError('请先选择要转换的帧/页')
    frame = 0 if frame is None else frame
    if type(frame) is not int or not 0 <= frame < frames:
        raise ValueError(f'帧/页位置无效，请选择第 1 至 {frames} 帧/页')
    image.seek(frame)
    width, height = _stored_size(image)
    if width * height > MAX_PIXELS:
        raise ValueError('所选帧/页不能超过 6000 万像素')
    return frame, frames


def preview_frame(source, frame):
    check_cancelled()
    with _open(source) as original:
        _select_frame(original, frame)
        image = original.convert('RGBA')
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
    check_cancelled()
    return buffer.getvalue()


def result_thumbnail(source):
    with _open(source) as image:
        image.thumbnail((RESULT_THUMB_EDGE, RESULT_THUMB_EDGE), Image.Resampling.LANCZOS)
        thumbnail = ImageOps.exif_transpose(image)
        buffer = io.BytesIO()
        thumbnail.save(buffer, format='WEBP', quality=82, method=2, icc_profile=image.info.get('icc_profile', b''))
        return buffer.getvalue()


def convert_image(source, destination, output_format, *, frame=None, quality=95, background='#ffffff'):
    output_format = output_format.lower()
    if output_format not in {'jpeg', 'png'}:
        raise ValueError('输出格式只能选择 JPEG 或 PNG')
    if not 1 <= quality <= 100 or not re.fullmatch(r'#[0-9a-fA-F]{6}', background):
        raise ValueError('转换参数无效')
    check_cancelled()
    destination = Path(destination)
    if Path(source).resolve() == destination.resolve():
        raise ValueError('转换结果不能覆盖原文件')
    notes = []
    try:
        with _open(source) as original:
            width, height = _stored_size(original)
            if width * height > MAX_PIXELS:
                raise ValueError('格式转换输入图片不能超过 6000 万像素，请先缩小图片')
            input_format = original.format
            frame, frames = _select_frame(original, frame)
            if frames > 1:
                notes.append(f'已转换所选第 {frame + 1} 帧/页（共 {frames} 帧/页）')
            check_cancelled()
            # Pillow applies and removes the current TIFF page's orientation
            # during load. Save its EXIF first and reverse the pixel transform.
            reverse = UNDO_TIFF_ORIENTATION.get(original.tag_v2.get(ORIENTATION)) if input_format == 'TIFF' else None
            exif = original.getexif().tobytes() if reverse is not None else None
            original.load()
            if reverse is not None and ORIENTATION not in original.tag_v2:
                image = original.transpose(reverse)
            else:
                image = original.copy()
            if exif is None and original.getexif():
                exif = original.getexif().tobytes()
            icc = image.info.get('icc_profile')
            alpha = image.convert('RGBA').getchannel('A') if ('A' in image.getbands() or 'transparency' in image.info) else None
            if image.mode in {'F'}:
                raise ValueError('暂不支持浮点像素图片，请先导出普通 PNG 或 TIFF')
            high_depth = image.mode.startswith('I')
            if high_depth and output_format == 'png':
                if image.mode == 'I':
                    low, high = image.getextrema()
                    if low < 0 or high > 65535:
                        raise ValueError('PNG 无法无损保存此整数范围，请先转换为 16 位或 8 位图片')
                    image = image.convert('I;16')
                icc = None
            else:
                if high_depth:
                    array = np.asarray(image, dtype=np.float64)
                    image = Image.fromarray(np.rint(np.clip(array, 0, 65535) / 257).astype(np.uint8), 'L')
                    notes.append('16 位灰度已转换为 JPEG 支持的 8 位灰度')
                    icc = None
                if icc and image.mode in {'CMYK', 'LAB', 'LA'}:
                    try:
                        color = image.convert('L') if image.mode == 'LA' else image
                        image = ImageCms.profileToProfile(color, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                         ImageCms.createProfile('sRGB'), outputMode='RGB')
                        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
                    except (OSError, ValueError, ImageCms.PyCMSError):
                        icc = None
                        notes.append('原图颜色配置不可用，已按普通 RGB 转换')
                if image.mode != 'L' or alpha is not None:
                    image = image.convert('RGB')
                if alpha is not None:
                    if output_format == 'png':
                        image.putalpha(alpha)
                    else:
                        flattened = Image.new('RGB', image.size, background)
                        flattened.paste(image, mask=alpha)
                        image = flattened
                        notes.append('JPEG 不支持透明背景，已填充所选底色')
            check_cancelled()
            # PNG can be larger than the uploaded/compressed source.
            ensure_space(destination.parent, image.width * image.height * 4 + 1024**2)
            options = {'icc_profile': icc} if icc else {}
            if exif:
                options['exif'] = exif
            if output_format == 'jpeg':
                options.update(quality=quality, subsampling=0)
            else:
                options.update(compress_level=6)
            if frames == 1 and input_format == output_format.upper():
                ensure_space(destination.parent, Path(source).stat().st_size)
                shutil.copyfile(source, destination)
            else:
                image.save(destination, format=output_format.upper(), **options)
            check_cancelled()
            return {'input_format': input_format, 'format': output_format, 'width': image.width,
                    'height': image.height, 'bytes': destination.stat().st_size, 'frame': frame, 'warnings': notes}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
