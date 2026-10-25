"""Persistencia mínima en SQLite.

Guarda el historial de cada conversación, el estado (bot activo / pausado)
y deduplica los webhooks que Meta reenvía cuando no recibe un 200 a tiempo.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    phone        TEXT PRIMARY KEY,
    display_name TEXT,
    channel      TEXT NOT NULL DEFAULT 'whatsapp',
    reply_to     TEXT,
    paused       INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id      TEXT UNIQUE,
    phone      TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id);
CREATE INDEX IF NOT EXISTS idx_conversations_seen ON conversations(last_seen_at);
"""

_lock = threading.RLock()
_db_path: Path | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(path: str | Path) -> None:
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connection() as con:
        con.executescript(_SCHEMA)
        _migrate(con)
        con.execute("PRAGMA journal_mode=WAL")


def _migrate(con: sqlite3.Connection) -> None:
    """Añade columnas nuevas a bases de datos creadas con versiones previas."""
    columns = {row["name"] for row in con.execute("PRAGMA table_info(conversations)")}
    if "channel" not in columns:
        con.execute(
            "ALTER TABLE conversations ADD COLUMN channel TEXT NOT NULL DEFAULT 'whatsapp'"
        )
    if "reply_to" not in columns:
        con.execute("ALTER TABLE conversations ADD COLUMN reply_to TEXT")


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise RuntimeError("La base de datos no está inicializada: llama a db.init()")
    con = sqlite3.connect(_db_path, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #
def register_incoming(
    wa_id: str,
    phone: str,
    content: str,
    name: str | None = None,
    channel: str = "whatsapp",
    reply_to: str | None = None,
) -> bool:
    """Guarda el mensaje del cliente. Devuelve False si ya estaba guardado."""
    ts = now_iso()
    with _lock, _connection() as con:
        con.execute(
            """
            INSERT INTO conversations
                (phone, display_name, channel, reply_to, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                display_name = COALESCE(conversations.display_name, excluded.display_name),
                channel = excluded.channel,
                reply_to = excluded.reply_to
            """,
            (phone, name, channel, reply_to, ts, ts),
        )
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO messages (wa_id, phone, role, content, created_at)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (wa_id, phone, content, ts),
        )
        return cursor.rowcount > 0


def add_message(phone: str, role: str, content: str) -> None:
    """Añade un mensaje al historial. role: assistant | human | user | note."""
    ts = now_iso()
    with _lock, _connection() as con:
        con.execute(
            """
            INSERT INTO messages (wa_id, phone, role, content, created_at)
            VALUES (NULL, ?, ?, ?, ?)
            """,
            (phone, role, content, ts),
        )
        con.execute(
            "UPDATE conversations SET last_seen_at = ? WHERE phone = ?", (ts, phone)
        )


def set_paused(phone: str, paused: bool, reason: str | None = None) -> None:
    ts = now_iso()
    with _lock, _connection() as con:
        con.execute(
            """
            INSERT INTO conversations (phone, paused, pause_reason, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                paused = excluded.paused,
                pause_reason = excluded.pause_reason,
                last_seen_at = excluded.last_seen_at
            """,
            (phone, 1 if paused else 0, reason if paused else None, ts, ts),
        )


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #
def get_history(phone: str, limit: int = 20) -> list[dict[str, str]]:
    """Últimos mensajes en orden cronológico."""
    with _lock, _connection() as con:
        rows = con.execute(
            """
            SELECT role, content FROM messages
            WHERE phone = ? AND role IN ('user', 'assistant', 'human')
            ORDER BY id DESC LIMIT ?
            """,
            (phone, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def is_paused(phone: str) -> bool:
    with _lock, _connection() as con:
        row = con.execute(
            "SELECT paused FROM conversations WHERE phone = ?", (phone,)
        ).fetchone()
    return bool(row and row["paused"])


def get_conversation(phone: str) -> dict[str, Any] | None:
    with _lock, _connection() as con:
        row = con.execute(
            "SELECT * FROM conversations WHERE phone = ?", (phone,)
        ).fetchone()
    return dict(row) if row else None


def list_conversations(
    paused: bool | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    query = "SELECT * FROM conversations"
    params: list[Any] = []
    if paused is not None:
        query += " WHERE paused = ?"
        params.append(1 if paused else 0)
    query += " ORDER BY last_seen_at DESC LIMIT ?"
    params.append(limit)

    with _lock, _connection() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_messages(phone: str, limit: int = 100) -> list[dict[str, Any]]:
    with _lock, _connection() as con:
        rows = con.execute(
            """
            SELECT id, role, content, created_at FROM messages
            WHERE phone = ? ORDER BY id DESC LIMIT ?
            """,
            (phone, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]
