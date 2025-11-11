from __future__ import annotations

import os
import datetime as dt
from typing import Optional, List

from sqlmodel import SQLModel, Field, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import sessionmaker

DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data.sqlite3")

class Bet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    text: str
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow())

class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bet_id: int = Field(index=True)
    remind_at: dt.datetime = Field(index=True)
    is_sent: bool = Field(default=False, index=True)

class OddsEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ext_id: str = Field(index=True)             # внешний ID события
    sport: Optional[str] = Field(default=None, index=True)
    league: Optional[str] = Field(default=None, index=True)
    team1: Optional[str] = Field(default=None, index=True)
    team2: Optional[str] = Field(default=None, index=True)
    starts_at: Optional[dt.datetime] = Field(default=None, index=True)
    odds1: Optional[float] = None
    oddsX: Optional[float] = None
    odds2: Optional[float] = None
    updated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.utcnow(), index=True)

_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[sessionmaker] = None

async def init_db() -> None:
    global _engine, _async_session_factory
    _engine = create_engine(DB_URL, echo=False, future=True).execution_options(async_=True)  # type: ignore
    async_engine = _engine.sync_engine.execution_options(async_=True)  # type: ignore

    from sqlalchemy.ext.asyncio import AsyncEngine as AE
    assert isinstance(async_engine, AE)
    _engine = async_engine

    async with _engine.begin() as conn:  # type: ignore
        await conn.run_sync(SQLModel.metadata.create_all)

    _async_session_factory = sessionmaker(
        bind=_engine, class_=AsyncSession, expire_on_commit=False  # type: ignore
    )

def async_session() -> AsyncSession:
    assert _async_session_factory is not None, "DB is not initialized"
    return _async_session_factory()

# CRUD helpers
async def upsert_events(events: List[OddsEvent]) -> int:
    if not events:
        return 0
    async with async_session() as s:
        inserted = 0
        for ev in events:
            q = select(OddsEvent).where(OddsEvent.ext_id == ev.ext_id)
            res = await s.exec(q)
            existed = res.one_or_none()
            if existed:
                existed.sport = ev.sport
                existed.league = ev.league
                existed.team1 = ev.team1
                existed.team2 = ev.team2
                existed.starts_at = ev.starts_at
                existed.odds1 = ev.odds1
                existed.oddsX = ev.oddsX
                existed.odds2 = ev.odds2
                existed.updated_at = dt.datetime.utcnow()
            else:
                s.add(ev)
                inserted += 1
        await s.commit()
        return inserted

async def search_events(query: Optional[str] = None, limit: int = 10):
    async with async_session() as s:
        q = select(OddsEvent).order_by(OddsEvent.starts_at.asc()).limit(limit)
        if query:
            like = f"%{query.lower()}%"
            q = (
                select(OddsEvent)
                .where(
                    (OddsEvent.league.ilike(like)) |
                    (OddsEvent.team1.ilike(like)) |
                    (OddsEvent.team2.ilike(like))
                )
                .order_by(OddsEvent.starts_at.asc())
                .limit(limit)
            )
        res = await s.exec(q)
        return res.all()

