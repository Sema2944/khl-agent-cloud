from sqlmodel import SQLModel, Field
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///./src/bets.db"

# Асинхронный движок для SQLite
engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Модель ставки
class Bet(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Модель напоминания
class Reminder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int
    text: str
    remind_at: datetime

# Инициализация базы (создание таблиц)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    print("[DB] Таблицы готовы.")

