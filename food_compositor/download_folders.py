"""Allocate stable download folder names for a workspace's batches."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

NUMBERED_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}(?:-[a-z0-9][a-z0-9._-]{0,31})?（[1-9]\d{0,8}）")


def allocate_download_folder(root: Path, owner: str, batch_id: str, day: date, username: str | None = None) -> str:
    day_key = day.isoformat()
    scope = f"account:{username}" if username else owner
    suffix = f"-{username}" if username else ""
    with closing(sqlite3.connect(root / "download-folders.sqlite3", timeout=10)) as database, database:
        # Acquire the writer lock before reading the last number. A retry of
        # the same batch reuses its name, even after a server restart.
        database.execute("BEGIN IMMEDIATE")
        database.execute("""
            CREATE TABLE IF NOT EXISTS download_folders (
                owner TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                day TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                PRIMARY KEY (owner, batch_id),
                UNIQUE (owner, day, sequence)
            )
        """)
        existing = database.execute(
            "SELECT day, sequence FROM download_folders WHERE owner = ? AND batch_id = ?",
            (scope, batch_id),
        ).fetchone()
        if existing:
            saved_day, sequence = existing
            return f"{saved_day}{suffix}（{sequence}）"
        if username:
            last = database.execute("SELECT MAX(sequence) FROM download_folders WHERE owner = ? AND day = ?", (scope, day_key))
        else:
            last = database.execute("SELECT MAX(sequence) FROM download_folders WHERE day = ? AND owner NOT LIKE 'account:%'", (day_key,))
        sequence = (last.fetchone()[0] or 0) + 1
        database.execute("INSERT INTO download_folders VALUES (?, ?, ?, ?)", (scope, batch_id, day_key, sequence))
        # Keep names for at least as long as the corresponding batch outputs.
        database.execute("DELETE FROM download_folders WHERE day < ?", ((day - timedelta(days=8)).isoformat(),))
        return f"{day_key}{suffix}（{sequence}）"
