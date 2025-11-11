from sqlmodel import SQLModel, Field, create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
import asyncio
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///./src/bets.db"

# Создаём асинхронный движок для SQLite
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Bet(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Reminder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int
    text: str
    remind_at: datetime


async def init_db():
    """Создание таблиц, если их ещё нет"""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    print("[DB] Таблицы готовы.")


# Тест при ручном запуске (необязательно)
if __name__ == "__main__":
    asyncio.run(init_db())
