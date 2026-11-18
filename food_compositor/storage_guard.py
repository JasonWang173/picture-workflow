"""Leave recovery space while uploading, composing and building downloads."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class StorageFull(OSError):
    pass


def reserve_bytes():
    return int(os.environ.get('MIN_FREE_DISK_MB', '2048')) * 1024 * 1024


def ensure_space(directory, required=0, *, include_temporary=False):
    paths = [Path(directory)]
    if include_temporary:
        paths.append(Path(tempfile.gettempdir()))
    if any(shutil.disk_usage(path).free < reserve_bytes() + max(0, required) for path in paths):
        raise StorageFull('服务器可用磁盘不足，已停止继续写入并保留已完成图片，请减少本批素材或清理过期任务后重试')
