from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass
class SessionState:
    state: str | None
    payload: dict[str, Any]
    last_outfit: str | None
    context: str | None


class Storage:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT,
                    payload TEXT,
                    last_outfit TEXT,
                    context TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    s3_key TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );
                """
            )
            await self._ensure_column(db, "photos", "s3_key", "TEXT")
            await db.commit()

    async def _ensure_column(self, db: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    async def ensure_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, is_active, created_at) VALUES (?, 1, ?)",
                (user_id, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def is_allowed(self, user_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT is_active FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return bool(row and row[0] == 1)

    async def add_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO users (user_id, is_active, created_at) VALUES (?, 1, ?)",
                (user_id, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def remove_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE users SET is_active = 0 WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

    async def list_users(self) -> list[int]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE is_active = 1")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_session(self, user_id: int) -> SessionState:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT state, payload, last_outfit, context FROM sessions WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return SessionState(state=None, payload={}, last_outfit=None, context=None)
            payload = json.loads(row[1]) if row[1] else {}
            return SessionState(state=row[0], payload=payload, last_outfit=row[2], context=row[3])

    async def update_session(
        self,
        user_id: int,
        state: str | None,
        payload: dict[str, Any],
        last_outfit: str | None = None,
        context: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO sessions (user_id, state, payload, last_outfit, context, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state = excluded.state,
                    payload = excluded.payload,
                    last_outfit = COALESCE(excluded.last_outfit, sessions.last_outfit),
                    context = COALESCE(excluded.context, sessions.context),
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    state,
                    json.dumps(payload, ensure_ascii=False),
                    last_outfit,
                    context,
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()

    async def clear_session(self, user_id: int) -> None:
        await self.update_session(user_id, None, {}, None, None)

    async def add_photo(self, user_id: int, file_id: str, s3_key: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO photos (user_id, file_id, s3_key, created_at) VALUES (?, ?, ?, ?)",
                (user_id, file_id, s3_key, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def list_recent_photos(self, user_id: int, limit: int = 6) -> list[dict[str, str]]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT file_id, s3_key FROM photos WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [{"file_id": row[0], "s3_key": row[1]} for row in rows]
