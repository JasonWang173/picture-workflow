"""OSS-backed storage for the composition history and recycle bin."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from threading import Lock
from .archive_snapshot import ArchiveSnapshot
from .record_cache import CatalogPending, RecordCache, read_records


RECORD_ID = re.compile(r"[0-9a-f]{12}-[0-9]{9,}")
OWNER_ID = re.compile(r"[0-9a-f]{64}")
RETENTION = timedelta(days=7)
LOGGER = logging.getLogger(__name__)
_locks = [Lock() for _ in range(64)]
_catalogs = RecordCache(ttl=60, max_stale=RETENTION.total_seconds())


def normalize_record(record):
    record = dict(record)
    record.setdefault("downloaded_at", record["saved_at"])
    record.setdefault("updated_at", record["saved_at"])
    record.setdefault("trashed_at", record["saved_at"] if record["section"] == "trash" else None)
    return record


def history_time(record):
    return record.get("downloaded_at") or record["saved_at"]


def history_timestamp(record):
    """Comparable UTC timestamp for directory ordering and pagination cursors."""
    return datetime.fromisoformat(history_time(record)).astimezone(timezone.utc).isoformat(timespec="microseconds")


def history_day(record):
    return datetime.fromisoformat(history_time(record)).astimezone(timezone(timedelta(hours=8))).date().isoformat()


class CompositionArchive:
    def __init__(self, bucket, sdk, prefix: str, *, cache_dir=None):
        self.bucket = bucket
        self.sdk = sdk
        self.prefix = prefix
        self.snapshot = None
        if cache_dir is not None:
            namespace = hashlib.sha256(self._cache_key(None).encode()).hexdigest()
            self.snapshot = ArchiveSnapshot(cache_dir / f"{namespace}.sqlite3")

    def _cache_key(self, owner):
        endpoint = getattr(self.bucket, "base_url", None) or getattr(self.bucket, "endpoint", "")
        return f"{getattr(self.bucket, 'bucket_name', getattr(self.bucket, 'bucket', id(self.bucket)))}:{endpoint}:{self.prefix}:{owner}"

    def _key(self, owner: str, entry_id: str) -> str:
        return f"{self.prefix}{owner}/{entry_id}"

    def _read(self, key: str) -> dict | None:
        try:
            result = self.bucket.get_object(key + ".json")
        except self.sdk.exceptions.NoSuchKey:
            return None
        try:
            return normalize_record(json.loads(result.read()))
        finally:
            close = getattr(result, "close", None)
            if close:
                close()

    def _write(self, key: str, record: dict) -> None:
        self.bucket.put_object(key + ".json", json.dumps(record, ensure_ascii=False))
        owner = key[len(self.prefix):].split("/", 1)[0]
        self._update_catalogs(owner, record["id"], record)

    def _update_catalogs(self, owner, entry_id, record):
        _catalogs.update(self._cache_key(owner), entry_id, record)
        relative = f"{owner}/{entry_id}"
        _catalogs.update(self._cache_key(None), relative, {**record, "id": relative} if record else None,
                         snapshot=self.snapshot)

    def save(self, owner: str, entry_id: str, image: dict, path, section: str, *, download_folder: str | None = None) -> dict:
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            now = datetime.now(timezone.utc)
            previous = self._read(key)
            if previous and datetime.fromisoformat(previous["expires_at"]) > now:
                folder_added = bool(download_folder and not previous.get("download_folder"))
                if folder_added:
                    previous["download_folder"] = download_folder
                if previous["section"] == section:
                    # Retrying a download neither duplicates it nor extends retention.
                    if folder_added:
                        self._write(key, previous)
                    return previous
                if section == "trash":
                    return self._move_to_trash(key, previous, now)
                record = {**previous, "section": "history", "updated_at": now.isoformat(),
                          "downloaded_at": previous.get("downloaded_at") or now.isoformat()}
                self._write(key, record)
                return record
            self.bucket.put_object_from_file(key + ".jpg", str(path))
            record = {
                "id": entry_id,
                "name": path.name,
                "product_name": image["product_name"],
                "template_name": image["template_name"],
                "section": section,
                "saved_at": now.isoformat(),
                "downloaded_at": now.isoformat() if section == "history" else None,
                "trashed_at": now.isoformat() if section == "trash" else None,
                "updated_at": now.isoformat(),
                "expires_at": (now + RETENTION).isoformat(),
            }
            if download_folder:
                record["download_folder"] = download_folder
            self._write(key, record)
            return record

    def _move_to_trash(self, key: str, record: dict, now: datetime) -> dict:
        if record["section"] != "trash":
            record = {**record, "section": "trash", "trashed_at": now.isoformat(), "updated_at": now.isoformat(),
                      "expires_at": (now + RETENTION).isoformat()}
            self._write(key, record)
        return record

    def trash(self, owner: str, entry_id: str) -> dict | None:
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            record = self.get(owner, entry_id)
            if record is None:
                return None
            return self._move_to_trash(key, record, datetime.now(timezone.utc))

    def get(self, owner: str, entry_id: str) -> dict | None:
        record = self._read(self._key(owner, entry_id))
        if record and datetime.fromisoformat(record["expires_at"]) > datetime.now(timezone.utc):
            return record
        return None

    def _catalog(self, owner, *, refresh=False, timeout=2):
        prefix = self.prefix if owner is None else f"{self.prefix}{owner}/"
        return _catalogs.get(self._cache_key(owner),
                             lambda old, versions: read_records(self.bucket, self.sdk, prefix, old, versions, RECORD_ID,
                                                                valid_parent=OWNER_ID if owner is None else None),
                             force=refresh, snapshot=self.snapshot, timeout=timeout)

    def warm(self):
        if self.snapshot is not None:
            try:
                self._catalog(None, timeout=0)
            except CatalogPending:
                pass  # The single background reader continues without blocking startup.
            except RuntimeError:
                LOGGER.warning("空间站目录暂时无法预热")

    def list(self, owner: str | None, *, refresh=False) -> list[dict]:
        shared_catalog = owner is None or self.snapshot is not None
        records, _ = self._catalog(None if shared_catalog else owner, refresh=refresh)
        now = datetime.now(timezone.utc)
        visible = []
        for raw in records:
            if datetime.fromisoformat(raw["expires_at"]) <= now:
                continue
            if shared_catalog:
                source, entry_id = raw["id"].split("/")
                if owner is not None and source != owner:
                    continue
            record = normalize_record(raw)
            if shared_catalog:
                record["id"] = entry_id
                if owner is None:
                    record["archive_key"] = f"{source}~{entry_id}"
            visible.append(record)
        return sorted(visible, key=lambda record: record["saved_at"], reverse=True)

    def restore(self, owner: str, entry_id: str) -> dict | None:
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            record = self.get(owner, entry_id)
            if record is None:
                return None
            if record["section"] == "trash":
                record["section"] = "history"
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                record["downloaded_at"] = record.get("downloaded_at") or record["updated_at"]
                self._write(key, record)
            return record

    def read_image(self, owner: str, entry_id: str, *, download: bool):
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            record = self.get(owner, entry_id)
            if record is None or (download and record["section"] != "history"):
                return None
            try:
                result = self.bucket.get_object(key + ".jpg")
            except self.sdk.exceptions.NoSuchKey:
                return None
            try:
                return record["name"], result.read()
            finally:
                close = getattr(result, "close", None)
                if close:
                    close()

    def purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        for obj in self.sdk.ObjectIteratorV2(self.bucket, prefix=self.prefix):
            relative = obj.key[len(self.prefix):]
            owner, _, filename = relative.partition("/")
            if not OWNER_ID.fullmatch(owner) or not RECORD_ID.fullmatch(filename.rsplit(".", 1)[0]):
                continue
            key = obj.key.rsplit(".", 1)[0]
            modified = getattr(obj, "last_modified", None)
            try:
                if filename.endswith(".json"):
                    etag = getattr(obj, "etag", None)
                    cached = _catalogs.peek(self._cache_key(None), relative[:-5], etag=etag) if etag else None
                    if cached and datetime.fromisoformat(cached["expires_at"]) > now:
                        continue
                    with _locks[hash(key) % len(_locks)]:
                        record = self._read(key)
                        if record and datetime.fromisoformat(record["expires_at"]) <= now:
                            self._delete(key + ".jpg")
                            self._delete(key + ".json")
                            self._update_catalogs(owner, record["id"], None)
                elif filename.endswith(".jpg") and modified is not None and now.timestamp() - modified >= RETENTION.total_seconds():
                    # Recover an upload interrupted before its metadata was written.
                    with _locks[hash(key) % len(_locks)]:
                        if self._read(key) is None:
                            self._delete(obj.key)
            except (self.sdk.exceptions.OssError, ValueError, KeyError):
                LOGGER.warning("合成记录清理失败：%s", filename, exc_info=True)

    def _delete(self, key: str) -> None:
        try:
            self.bucket.delete_object(key)
        except self.sdk.exceptions.NoSuchKey:
            pass
