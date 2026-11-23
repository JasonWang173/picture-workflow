"""Local team accounts and revocable, server-side login sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

SESSION_SECONDS = 7 * 24 * 3600
_INITIALS = re.compile(r"[A-Za-z]{1,32}")
# Older accounts can contain digits and punctuation. They still work for sign-in and maintenance.
_EXISTING_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_DUMMY_SALT = b"\0" * 16


class DuplicateAccount(ValueError):
    pass


def accounts_path() -> Path:
    default = Path(__file__).resolve().parent.parent / "runtime" / "accounts.sqlite3"
    return Path(os.environ.get("WORKFLOW_ACCOUNTS_DB", default))


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2, dklen=32)


def _token_hash(token: str) -> str | None:
    return hashlib.sha256(token.encode("ascii")).hexdigest() if _TOKEN.fullmatch(token) else None


def _validated_username(username: str) -> str:
    username = username.strip()
    if not _EXISTING_USERNAME.fullmatch(username):
        raise ValueError("请输入有效的账号")
    return username.lower()


def _new_password(password: str) -> tuple[bytes, bytes]:
    if not 15 <= len(password) <= 128:
        raise ValueError("密码需为 15–128 个字符")
    salt = secrets.token_bytes(16)
    return salt, _password_hash(password, salt)


class AccountStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with closing(sqlite3.connect(self.path, timeout=5)) as database:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys = ON")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_session_expiry ON account_sessions(expires_at);
            """)
            with database:
                yield database

    def create(self, username: str, display_name: str, password: str, *,
               previous_token: str | None = None) -> tuple[dict, str] | None:
        username = username.strip()
        if not _INITIALS.fullmatch(username):
            raise ValueError("请输入自己的英文缩写，仅限 1–32 个英文字母")
        username = username.lower()
        display_name = display_name.strip() or username
        if len(display_name) > 40 or not display_name.isprintable():
            raise ValueError("显示名称需为不超过 40 个可打印字符")
        salt, digest = _new_password(password)
        with self._connect() as database:
            try:
                inserted = database.execute("INSERT INTO accounts(username, display_name, salt, password_hash) VALUES (?, ?, ?, ?)",
                                            (username, display_name, salt, digest))
            except sqlite3.IntegrityError:
                raise DuplicateAccount("该缩写已被使用，可以多加一个英文字母区分") from None
            if previous_token is not None:
                token = secrets.token_hex(32)
                now = int(time.time())
                database.execute("INSERT INTO account_sessions VALUES (?, ?, ?)",
                                 (_token_hash(token), inserted.lastrowid, now + SESSION_SECONDS))
                self._retire_sessions(database, now, previous_token)
                return {"username": username, "display_name": display_name}, token
        return None

    @staticmethod
    def _retire_sessions(database, now: int, previous_token: str) -> None:
        database.execute("DELETE FROM account_sessions WHERE expires_at <= ? OR token_hash = ?",
                         (now, _token_hash(previous_token)))

    def list_accounts(self) -> list[dict]:
        with self._connect() as database:
            return [dict(row) for row in database.execute(
                "SELECT username, display_name, enabled FROM accounts ORDER BY username"
            )]

    def reset_password(self, username: str, password: str) -> None:
        username = _validated_username(username)
        salt, digest = _new_password(password)
        with self._connect() as database:
            changed = database.execute("UPDATE accounts SET salt = ?, password_hash = ? WHERE username = ?",
                                       (salt, digest, username))
            if not changed.rowcount:
                raise ValueError("账号不存在")
            database.execute("DELETE FROM account_sessions WHERE account_id = (SELECT id FROM accounts WHERE username = ?)",
                             (username,))

    def set_enabled(self, username: str, enabled: bool) -> None:
        username = _validated_username(username)
        with self._connect() as database:
            changed = database.execute("UPDATE accounts SET enabled = ? WHERE username = ?", (int(enabled), username))
            if not changed.rowcount:
                raise ValueError("账号不存在")
            if not enabled:
                database.execute("DELETE FROM account_sessions WHERE account_id = (SELECT id FROM accounts WHERE username = ?)",
                                 (username,))

    def sign_in(self, username: str, password: str, previous_token: str = "") -> tuple[dict, str] | None:
        username = username.strip()
        if not _EXISTING_USERNAME.fullmatch(username) or not 1 <= len(password) <= 128:
            return None
        username = username.lower()
        with self._connect() as database:
            account = database.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
            salt = account["salt"] if account else _DUMMY_SALT
            digest = _password_hash(password, salt)
            expected = account["password_hash"] if account else b"\0" * 32
            if not hmac.compare_digest(digest, expected) or not account or not account["enabled"]:
                return None
            token = secrets.token_hex(32)
            now = int(time.time())
            # Verify the account still has the same credentials at session creation.
            inserted = database.execute("""
                INSERT INTO account_sessions(token_hash, account_id, expires_at)
                SELECT ?, id, ? FROM accounts WHERE id = ? AND enabled = 1 AND password_hash = ?
            """, (_token_hash(token), now + SESSION_SECONDS, account["id"], expected))
            if not inserted.rowcount:
                return None
            self._retire_sessions(database, now, previous_token)
            return {"username": account["username"], "display_name": account["display_name"]}, token

    def session(self, token: str) -> dict | None:
        digest = _token_hash(token)
        if digest is None or not self.path.is_file():
            return None
        with self._connect() as database:
            account = database.execute("""
                SELECT accounts.username, accounts.display_name
                FROM account_sessions JOIN accounts ON accounts.id = account_sessions.account_id
                WHERE token_hash = ? AND expires_at > ? AND enabled = 1
            """, (digest, int(time.time()))).fetchone()
            return dict(account) if account else None

    def sign_out(self, token: str) -> None:
        digest = _token_hash(token)
        if digest is not None and self.path.is_file():
            with self._connect() as database:
                database.execute("DELETE FROM account_sessions WHERE token_hash = ?", (digest,))
