"""Local metadata for one OSS archive namespace. Photos stay in OSS."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class ArchiveSnapshot:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN")
            stamp, = db.execute("SELECT synced_at FROM catalog").fetchone()
            records, versions = [], {}
            for record_id, etag, payload in db.execute("SELECT id, etag, payload FROM records"):
                records.append(json.loads(payload))
                if etag:
                    versions[record_id] = etag
            return stamp, records, versions

    def save(self, records, versions, *, changed):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS catalog (synced_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, etag TEXT, payload TEXT NOT NULL)")
            if changed or not db.execute("SELECT 1 FROM catalog").fetchone():
                db.execute("DELETE FROM records")
                db.executemany("INSERT INTO records VALUES (?, ?, ?)", (
                    (key, versions.get(key), json.dumps(record, ensure_ascii=False)) for key, record in records.items()
                ))
            db.execute("DELETE FROM catalog")
            db.execute("INSERT INTO catalog VALUES (?)", (time.time(),))

    def update(self, record_id, record):
        if not self.path.is_file():
            return  # A complete initial listing establishes the snapshot.
        with closing(sqlite3.connect(self.path)) as db, db:
            if record is None:
                db.execute("DELETE FROM records WHERE id = ?", (record_id,))
            else:
                db.execute("INSERT OR REPLACE INTO records VALUES (?, NULL, ?)",
                           (record_id, json.dumps(record, ensure_ascii=False)))
