"""OSS-backed storage for the composition history and recycle bin."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock


RECORD_ID = re.compile(r"[0-9a-f]{12}-[0-9]{9,}")
OWNER_ID = re.compile(r"[0-9a-f]{64}")
RETENTION = timedelta(days=7)
LOGGER = logging.getLogger(__name__)
_locks = [Lock() for _ in range(64)]


class CompositionArchive:
    def __init__(self, bucket, sdk, prefix: str):
        self.bucket = bucket
        self.sdk = sdk
        self.prefix = prefix

    def _key(self, owner: str, entry_id: str) -> str:
        return f"{self.prefix}{owner}/{entry_id}"

    def _read(self, key: str) -> dict | None:
        try:
            result = self.bucket.get_object(key + ".json")
        except self.sdk.exceptions.NoSuchKey:
            return None
        try:
            return json.loads(result.read())
        finally:
            close = getattr(result, "close", None)
            if close:
                close()

    def _write(self, key: str, record: dict) -> None:
        self.bucket.put_object(key + ".json", json.dumps(record, ensure_ascii=False))

    def save(self, owner: str, entry_id: str, image: dict, path, section: str) -> dict:
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            now = datetime.now(timezone.utc)
            previous = self._read(key)
            if previous and datetime.fromisoformat(previous["expires_at"]) > now:
                if previous["section"] == section:
                    # Retrying a download neither duplicates it nor extends retention.
                    return previous
                if section == "trash":
                    return self._move_to_trash(key, previous, now)
                record = {**previous, "section": "history"}
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
                "expires_at": (now + RETENTION).isoformat(),
            }
            self._write(key, record)
            return record

    def _move_to_trash(self, key: str, record: dict, now: datetime) -> dict:
        if record["section"] != "trash":
            record = {**record, "section": "trash", "saved_at": now.isoformat(),
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

    def list(self, owner: str) -> list[dict]:
        prefix = f"{self.prefix}{owner}/"
        entry_ids = [obj.key[len(prefix):-5] for obj in self.sdk.ObjectIteratorV2(self.bucket, prefix=prefix)
                     if obj.key.endswith(".json") and RECORD_ID.fullmatch(obj.key[len(prefix):-5])]
        with ThreadPoolExecutor(max_workers=6) as pool:
            records = [record for record in pool.map(lambda entry_id: self.get(owner, entry_id), entry_ids)
                       if record is not None]
        return sorted(records, key=lambda record: record["saved_at"], reverse=True)

    def restore(self, owner: str, entry_id: str) -> dict | None:
        key = self._key(owner, entry_id)
        with _locks[hash(key) % len(_locks)]:
            record = self.get(owner, entry_id)
            if record is None:
                return None
            if record["section"] == "trash":
                record["section"] = "history"
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
                    with _locks[hash(key) % len(_locks)]:
                        record = self._read(key)
                        if record and datetime.fromisoformat(record["expires_at"]) <= now:
                            self._delete(key + ".jpg")
                            self._delete(key + ".json")
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
