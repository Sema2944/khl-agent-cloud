# src/bets_db.py
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

DB_PATH = Path("bets.sqlite3")


@dataclass
class UserBet:
    id: int
    user_id: int
    created_at: datetime
    description: str


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            description TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def add_bet(user_id: int, description: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bets (user_id, created_at, description)
        VALUES (?, ?, ?)
        """,
        (user_id, datetime.utcnow().isoformat(), description),
    )
    conn.commit()
    conn.close()


def clear_bets(user_id: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM bets WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def list_bets(user_id: int) -> List[UserBet]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, created_at, description "
        "FROM bets WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        UserBet(
            id=row["id"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            description=row["description"],
        )
        for row in rows
    ]
