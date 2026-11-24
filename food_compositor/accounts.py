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
REMEMBER_SECONDS = 30 * 24 * 3600
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
    if not password:
        raise ValueError("请输入密码")
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
                CREATE TABLE IF NOT EXISTS remembered_devices (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remembered_expiry ON remembered_devices(expires_at);
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    device_hash TEXT REFERENCES remembered_devices(token_hash) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS account_session_expiry ON account_sessions(expires_at);
            """)
            with database:
                if "device_hash" not in {r["name"] for r in database.execute("PRAGMA table_info(account_sessions)")}:
                    database.execute("BEGIN IMMEDIATE")
                    if "device_hash" not in {r["name"] for r in database.execute("PRAGMA table_info(account_sessions)")}:
                        database.execute("ALTER TABLE account_sessions ADD COLUMN device_hash TEXT REFERENCES remembered_devices(token_hash) ON DELETE CASCADE")
                    database.commit()
                yield database

    def create(self, username: str, display_name: str, password: str, *,
               previous_token: str | None = None, previous_device: str = "", new_device: str = "") -> tuple[dict, str] | None:
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
                token = self._new_login(database, inserted.lastrowid, previous_token, previous_device, new_device)
                return {"username": username, "display_name": display_name}, token
        return None

    @staticmethod
    def _new_login(database, account_id: int, previous_token: str, previous_device: str, new_device: str) -> str:
        now = int(time.time())
        database.execute("DELETE FROM remembered_devices WHERE expires_at <= ? OR token_hash = ?",
                         (now, _token_hash(previous_device)))
        database.execute("DELETE FROM account_sessions WHERE expires_at <= ? OR token_hash = ?",
                         (now, _token_hash(previous_token)))
        device_hash = _token_hash(new_device)
        if device_hash:
            database.execute("INSERT INTO remembered_devices VALUES (?, ?, ?)",
                             (device_hash, account_id, now + REMEMBER_SECONDS))
        return AccountStore._issue_session(database, account_id, device_hash, now + SESSION_SECONDS)

    @staticmethod
    def _issue_session(database, account_id: int, device_hash: str | None, expires_at: int) -> str:
        token = secrets.token_hex(32)
        database.execute("INSERT INTO account_sessions(token_hash, account_id, expires_at, device_hash) VALUES (?, ?, ?, ?)",
                         (_token_hash(token), account_id, expires_at, device_hash))
        return token

    def list_accounts(self) -> list[dict]:
        with self._connect() as database:
            return [dict(row) for row in database.execute(
                "SELECT username, display_name, enabled FROM accounts ORDER BY username"
            )]

    @staticmethod
    def _revoke_account_logins(database, username: str) -> None:
        database.execute("DELETE FROM remembered_devices WHERE account_id = (SELECT id FROM accounts WHERE username = ?)",
                         (username,))
        database.execute("DELETE FROM account_sessions WHERE account_id = (SELECT id FROM accounts WHERE username = ?)",
                         (username,))

    def reset_password(self, username: str, password: str) -> None:
        username = _validated_username(username)
        salt, digest = _new_password(password)
        with self._connect() as database:
            changed = database.execute("UPDATE accounts SET salt = ?, password_hash = ? WHERE username = ?",
                                       (salt, digest, username))
            if not changed.rowcount:
                raise ValueError("账号不存在")
            self._revoke_account_logins(database, username)

    def change_password(self, token: str, username: str, current_password: str, new_password: str) -> bool:
        """Change only the account owning this session. False means the session is no longer valid."""
        token_hash = _token_hash(token)
        if token_hash is None or not self.path.is_file():
            return False
        with self._connect() as database:
            account = database.execute("""
                SELECT a.salt, a.password_hash FROM accounts a JOIN account_sessions s ON a.id = s.account_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND a.username = ? AND a.enabled = 1
            """, (token_hash, int(time.time()), username)).fetchone()
            if not account:
                return False
            if not hmac.compare_digest(_password_hash(current_password, account["salt"]), account["password_hash"]):
                raise ValueError("原密码不正确")
            if new_password == current_password:
                raise ValueError("新密码须与原密码不同")
            salt, digest = _new_password(new_password)
            # Compute the expensive hashes before acquiring the write lock, then recheck both credentials.
            database.execute("BEGIN IMMEDIATE")
            updated = database.execute("""
                UPDATE accounts SET salt = ?, password_hash = ?
                WHERE username = ? AND enabled = 1 AND password_hash = ?
                AND id IN (SELECT account_id FROM account_sessions WHERE token_hash = ? AND expires_at > ?)
            """, (salt, digest, username, account["password_hash"], token_hash, int(time.time())))
            if not updated.rowcount:
                return False
            self._revoke_account_logins(database, username)
            return True

    def set_enabled(self, username: str, enabled: bool) -> None:
        username = _validated_username(username)
        with self._connect() as database:
            changed = database.execute("UPDATE accounts SET enabled = ? WHERE username = ?", (int(enabled), username))
            if not changed.rowcount:
                raise ValueError("账号不存在")
            if not enabled:
                self._revoke_account_logins(database, username)

    def sign_in(self, username: str, password: str, previous_token: str = "", *,
                previous_device: str = "", new_device: str = "") -> tuple[dict, str] | None:
        username = username.strip()
        if not _EXISTING_USERNAME.fullmatch(username) or not password:
            return None
        username = username.lower()
        with self._connect() as database:
            account = database.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
            salt = account["salt"] if account else _DUMMY_SALT
            digest = _password_hash(password, salt)
            expected = account["password_hash"] if account else b"\0" * 32
            if not hmac.compare_digest(digest, expected) or not account or not account["enabled"]:
                return None
            # Keep credential changes, device replacement and session creation atomic.
            database.execute("BEGIN IMMEDIATE")
            if not database.execute("SELECT 1 FROM accounts WHERE id = ? AND enabled = 1 AND password_hash = ?",
                                    (account["id"], expected)).fetchone():
                return None
            token = self._new_login(database, account["id"], previous_token, previous_device, new_device)
            return {"username": account["username"], "display_name": account["display_name"]}, token

    @staticmethod
    def _device_account(database, device_hash, now):
        return database.execute("""
            SELECT a.id, a.username, a.display_name, d.expires_at FROM remembered_devices d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.token_hash = ? AND d.expires_at > ? AND a.enabled = 1
        """, (device_hash, now)).fetchone()

    def remembered_account(self, device: str) -> dict | None:
        digest = _token_hash(device)
        if digest is None or not self.path.is_file():
            return None
        with self._connect() as database:
            row = self._device_account(database, digest, int(time.time()))
            return {"username": row["username"], "display_name": row["display_name"]} if row else None

    def resume(self, device: str, username: str, previous_token: str) -> tuple[dict, str] | None:
        digest = _token_hash(device)
        if digest is None or not self.path.is_file():
            return None
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = self._device_account(database, digest, now)
            if not row or row["username"] != username:
                return None
            database.execute("DELETE FROM account_sessions WHERE expires_at <= ? OR token_hash = ?",
                             (now, _token_hash(previous_token)))
            token = self._issue_session(database, row["id"], digest, min(now + SESSION_SECONDS, row["expires_at"]))
            return {"username": row["username"], "display_name": row["display_name"]}, token

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

    def sign_out(self, token: str, *, device: str = "", forget: bool = True) -> None:
        digest = _token_hash(token)
        device_hash = _token_hash(device)
        if (digest or device_hash) and self.path.is_file():
            with self._connect() as database:
                database.execute("DELETE FROM account_sessions WHERE token_hash = ? OR device_hash = ?", (digest, device_hash))
                if forget:
                    database.execute("DELETE FROM remembered_devices WHERE token_hash = ?", (device_hash,))
