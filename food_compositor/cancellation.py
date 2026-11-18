"""Cooperative cancellation scoped to one composition, never the shared pool."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class CompositionCancelled(Exception):
    pass


_marker = ContextVar('composition_cancel_marker', default=None)


def check_cancelled():
    marker = _marker.get()
    if marker is not None and marker.exists():
        raise CompositionCancelled('任务已取消')


@contextmanager
def cancellation_scope(marker):
    token = _marker.set(Path(marker))
    try:
        check_cancelled()
        yield
        check_cancelled()
    finally:
        _marker.reset(token)
