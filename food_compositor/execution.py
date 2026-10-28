"""One bounded, reusable composition pool shared by all HTTP requests."""
from __future__ import annotations

import multiprocessing
import logging
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

MIB = 1024 * 1024


def cpu_limit():
    count = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max':
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return max(1, count)


def memory_budget():
    available = 2048 * MIB
    try:
        values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        available = int(values['MemAvailable'].split()[0]) * 1024
    except (OSError, ValueError, KeyError):
        pass
    try:
        maximum = int(Path('/sys/fs/cgroup/memory.max').read_text())
        current = int(Path('/sys/fs/cgroup/memory.current').read_text())
        available = min(available, maximum - current)
    except (OSError, ValueError):
        pass
    return max(512 * MIB, available - 512 * MIB)


def worker_count():
    if _workers is not None:
        return _workers
    # Per-worker native image buffers and optional inference session need headroom.
    automatic = max(1, min(cpu_limit(), memory_budget() // (768 * MIB), 4))
    try:
        requested = int(os.environ.get('BATCH_WORKERS', '0'))
    except ValueError:
        requested = 0
    return min(automatic, requested) if requested > 0 else automatic


def estimate_memory(product, template, size):
    from PIL import Image
    def pixels(path):
        if path is None:
            return 0
        try:
            with Image.open(path) as image:
                return image.width * image.height
        except (OSError, ValueError, Image.DecompressionBombError):
            return 0
    try:
        width, height = size.lower().split('x') if isinstance(size, str) else size
        output_pixels = max(0, int(width) * int(height))
    except (ValueError, TypeError):
        output_pixels = 1080 * 1920
    return 256 * MIB + pixels(template) * 64 + pixels(product) * 16 + output_pixels * 96


def _init_worker():
    try:
        import cv2
        cv2.setNumThreads(1)
    except ImportError:
        pass


_lock = threading.Lock()
_condition = threading.Condition()
_pool = None
_active = 0
_reserved = 0
_budget = None
_workers = None


def submit_composition(function, *args, estimated_bytes=1024 * MIB, **kwargs):
    global _pool, _active, _reserved, _budget, _workers
    with _condition:
        if _budget is None:
            _budget, _workers = memory_budget(), worker_count()
        reservation = max(1, min(estimated_bytes, _budget))
        _condition.wait_for(lambda: _active < _workers and (_active == 0 or _reserved + reservation <= _budget))
        _active += 1
        _reserved += reservation
    def release(_future=None):
        global _active, _reserved
        with _condition:
            _active -= 1
            _reserved -= reservation
            _condition.notify_all()
    try:
        with _lock:
            if _pool is None:
                _pool = ProcessPoolExecutor(max_workers=_workers, mp_context=multiprocessing.get_context('spawn'), initializer=_init_worker)
                logging.getLogger('uvicorn.error').info('合成进程池启动：workers=%s，内存预算=%s MiB', _workers, _budget // MIB)
            try:
                future = _pool.submit(function, *args, **kwargs)
            except BrokenProcessPool:
                _pool.shutdown(wait=False)
                _pool = None
                raise
    except BaseException:
        release()
        raise
    future.add_done_callback(release)
    return future


def shutdown_executor():
    global _pool, _budget, _workers
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True)
    with _condition:
        _budget = _workers = None
