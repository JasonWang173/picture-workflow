"""Bounded single-flight metadata snapshots; OSS remains the source of truth."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LOGGER = logging.getLogger(__name__)
_refresh_slots = threading.BoundedSemaphore(2)


class RecordCache:
    def __init__(self, ttl=60, max_stale=3600, capacity=128):
        self.ttl, self.max_stale, self.capacity = ttl, max_stale, capacity
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def get(self, key, loader, *, force=False, disk: Path | None = None, timeout=12):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                while len(self.entries) >= self.capacity:
                    removable = next((name for name, value in self.entries.items() if value["pending"] is None), None)
                    if removable is None:
                        raise RuntimeError("目录任务繁忙，请稍后重试")
                    del self.entries[removable]
                entry = {"records": None, "versions": {}, "at": 0, "retry": 0, "pending": None, "changes": {}}
                if disk is not None:
                    try:
                        saved = json.loads(disk.read_text(encoding="utf-8"))
                        if saved["key"] == key and time.time() - saved["at"] < self.max_stale:
                            age = max(self.ttl, time.time() - saved["at"])
                            entry.update(records={item["id"]: item for item in saved["records"]}, versions=saved["versions"], at=time.monotonic() - age)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                self.entries[key] = entry
            self.entries.move_to_end(key)
            age = time.monotonic() - entry["at"]
            usable = entry["records"] is not None and age < self.max_stale
            if not force and usable and age < self.ttl:
                return list(entry["records"].values()), False
            if entry["pending"] is None and (force or time.monotonic() >= entry["retry"]) and _refresh_slots.acquire(blocking=False):
                event = threading.Event()
                entry.update(pending=event, changes={})
                old = list((entry["records"] or {}).values())
                threading.Thread(target=self._refresh, args=(key, entry, loader, event, disk, old, entry["versions"].copy()), daemon=True).start()
            event = entry["pending"]
            if usable and not force:
                return list(entry["records"].values()), True
        if event is not None:
            event.wait(timeout)
        with self.lock:
            if entry["records"] is not None and time.monotonic() - entry["at"] < self.max_stale:
                return list(entry["records"].values()), bool(entry["pending"] or entry.get("error"))
            raise RuntimeError("云端目录读取超时或暂时不可用，请稍后重试")

    def _refresh(self, key, entry, loader, event, disk, old, old_versions):
        try:
            records, versions = loader(old, old_versions)
            with self.lock:
                merged = {item["id"]: item for item in records}
                for record_id, item in entry["changes"].items():
                    if item is None:
                        merged.pop(record_id, None)
                    else:
                        merged[record_id] = item
                    versions.pop(record_id, None)
                entry.update(records=merged, versions=versions, at=time.monotonic(), error=False)
                if disk is not None:
                    try:
                        disk.parent.mkdir(parents=True, exist_ok=True)
                        temporary = disk.with_suffix(".tmp")
                        temporary.write_text(json.dumps({"key": key, "at": time.time(), "records": list(merged.values()), "versions": versions}), encoding="utf-8")
                        temporary.replace(disk)
                    except OSError:
                        LOGGER.warning("目录缓存未能写入磁盘")
        except Exception:
            LOGGER.warning("云端目录刷新失败，保留上次成功快照", exc_info=True)
            with self.lock:
                entry.update(error=True, retry=time.monotonic() + 10)
        finally:
            with self.lock:
                entry["pending"] = None
                while len(self.entries) > self.capacity:
                    removable = next((name for name, value in self.entries.items() if value["pending"] is None and name != key), None)
                    if removable is None:
                        break
                    del self.entries[removable]
            event.set()
            _refresh_slots.release()

    def update(self, key, record_id, item):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return
            if entry["pending"] is not None:
                entry["changes"][record_id] = item
            if entry["records"] is not None:
                records = entry["records"]
                if item is None:
                    records.pop(record_id, None)
                else:
                    records[record_id] = item
                entry["versions"].pop(record_id, None)

    def peek(self, key, record_id):
        with self.lock:
            entry = self.entries.get(key)
            if entry and entry["records"] is not None and time.monotonic() - entry["at"] < self.max_stale:
                record = entry["records"].get(record_id)
                return dict(record) if record else None
        return None


def read_records(bucket, sdk, prefix, previous, versions, valid_id, *, valid_parent=None):
    """Only download changed JSON objects when OSS supplies their ETags."""
    old = {record["id"]: record for record in previous}
    objects = []
    for obj in sdk.ObjectIteratorV2(bucket, prefix=prefix):
        if not obj.key.endswith(".json"):
            continue
        relative = obj.key[len(prefix):-5]
        parent, _, leaf = relative.rpartition("/")
        if valid_parent is not None and not valid_parent.fullmatch(parent):
            continue
        if valid_id.fullmatch(leaf if valid_parent is not None else relative):
            objects.append((obj.key, relative, getattr(obj, "etag", None)))

    def read(obj):
        key, record_id, etag = obj
        if etag and versions.get(record_id) == etag and record_id in old:
            return old[record_id]
        try:
            response = bucket.get_object(key)
        except sdk.exceptions.NoSuchKey:
            return None  # a concurrent delete is normal
        try:
            record = json.loads(response.read())
        finally:
            response.close()
        if not isinstance(record, dict) or record.get("id") != record_id.rsplit("/", 1)[-1]:
            raise ValueError("云端记录格式不正确")
        return {**record, "id": record_id}

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = [record for record in pool.map(read, objects) if record is not None]
    return records, {record_id: etag for _, record_id, etag in objects if etag}
