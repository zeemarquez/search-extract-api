"""Encrypted, persistent session store (SQLite file + Fernet encryption).

This is the only persistent state of the service. Session contents are encrypted at
rest with Fernet (AES-128-CBC + HMAC-SHA256); metadata needed for listing (domain,
owner hash, status, dates) is stored in clear. Keys can be rotated by listing several
comma-separated keys in ``SESSION_ENCRYPTION_KEY`` (the first one encrypts).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from ..errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    label TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    cookie_count INTEGER NOT NULL,
    origin_count INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL,
    last_checked_at REAL,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_owner ON sessions(owner_id);
"""

_COLUMNS = (
    "id, owner_id, domain, label, source, status, status_reason, cookie_count, origin_count, "
    "expires_at, created_at, updated_at, last_used_at, last_checked_at"
)


class SessionCrypto:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("at least one encryption key is required")
        try:
            self._fernet = MultiFernet([Fernet(k.strip().encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "SESSION_ENCRYPTION_KEY must be Fernet keys (urlsafe base64, 32 bytes). "
                'Generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise ApiError(
                ErrorCode.SESSION_INVALID,
                "The session cannot be decrypted (was the encryption key changed?).",
            ) from exc

    @classmethod
    def from_settings(cls, configured: str | None, db_path: str) -> SessionCrypto:
        if configured and configured.strip():
            return cls([k for k in configured.split(",") if k.strip()])
        key_path = Path(db_path).expanduser().resolve().parent / "session.key"
        if key_path.exists():
            return cls([key_path.read_text(encoding="utf-8").strip()])
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key().decode()
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
        logger.warning(
            "SESSION_ENCRYPTION_KEY not set: generated a key at %s. Back it up, or set "
            "SESSION_ENCRYPTION_KEY explicitly (losing it makes stored sessions unreadable).",
            key_path,
        )
        return cls([key])


@dataclass
class SessionRow:
    id: str
    owner_id: str
    domain: str
    label: str | None
    source: str
    status: str
    status_reason: str | None
    cookie_count: int
    origin_count: int
    expires_at: float | None
    created_at: float
    updated_at: float
    last_used_at: float | None
    last_checked_at: float | None


class SessionStore:
    def __init__(self, db_path: str, crypto: SessionCrypto) -> None:
        self.db_path = db_path
        self.crypto = crypto
        self._lock = asyncio.Lock()
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        if db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if conn is not self._memory_conn:
            conn.close()

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            self._close(conn)
        if self.db_path != ":memory:":
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:  # pragma: no cover - e.g. Windows / read-only FS
                pass

    async def _run(self, fn: Any, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------ sync helpers
    def _insert(self, row: SessionRow, payload: bytes) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO sessions ({_COLUMNS}, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.id,
                    row.owner_id,
                    row.domain,
                    row.label,
                    row.source,
                    row.status,
                    row.status_reason,
                    row.cookie_count,
                    row.origin_count,
                    row.expires_at,
                    row.created_at,
                    row.updated_at,
                    row.last_used_at,
                    row.last_checked_at,
                    self.crypto.encrypt(payload),
                ),
            )
            conn.commit()
        finally:
            self._close(conn)

    def _get(self, session_id: str) -> tuple[SessionRow, bytes] | None:
        conn = self._connect()
        try:
            cur = conn.execute(f"SELECT {_COLUMNS}, data FROM sessions WHERE id = ?", (session_id,))
            found = cur.fetchone()
        finally:
            self._close(conn)
        if found is None:
            return None
        return SessionRow(*found[:-1]), found[-1]

    def _list(self, owner_id: str) -> list[SessionRow]:
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            )
            return [SessionRow(*r) for r in cur.fetchall()]
        finally:
            self._close(conn)

    def _update(self, session_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id)
            )
            conn.commit()
        finally:
            self._close(conn)

    def _delete(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._close(conn)

    def _count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        finally:
            self._close(conn)

    # ------------------------------------------------------------------ async API
    async def insert(self, row: SessionRow, payload: bytes) -> None:
        await self._run(self._insert, row, payload)

    async def get(self, session_id: str) -> tuple[SessionRow, bytes] | None:
        """Returns the metadata row and the *decrypted* payload."""
        found = await self._run(self._get, session_id)
        if found is None:
            return None
        row, blob = found
        return row, self.crypto.decrypt(blob)

    async def get_row(self, session_id: str) -> SessionRow | None:
        found = await self._run(self._get, session_id)
        return found[0] if found else None

    async def list(self, owner_id: str) -> list[SessionRow]:
        return await self._run(self._list, owner_id)

    async def update(self, session_id: str, **fields: Any) -> None:
        if "data" in fields:
            fields["data"] = self.crypto.encrypt(fields["data"])
            fields.setdefault("updated_at", time.time())
        await self._run(self._update, session_id, fields)

    async def delete(self, session_id: str) -> bool:
        return await self._run(self._delete, session_id)

    async def count(self) -> int:
        return await self._run(self._count)
