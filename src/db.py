# src/db.py
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()


def _normalize_db_url(url: str) -> str:
    u = (url or "").strip()
    # Render иногда даёт postgres:// — это алиас, лучше привести к postgresql://
    if u.startswith("postgres://"):
        u = u.replace("postgres://", "postgresql://", 1)

    # Если Postgres и драйвер не указан — форсим psycopg v3
    if u.startswith("postgresql://") and "+psycopg" not in u:
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)

    return u


DATABASE_URL = _normalize_db_url(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _is_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql")


def _bootstrap_migrations_postgres() -> None:
    """
    Мини-миграции без Alembic (MVP):
    - добавляем недостающие колонки в users
    - приводим типы к BIGINT там, где нужны Telegram ID
    - добавляем индексы
    - фиксируем sequence users.id после ручных id (id=tg_user_id)
    """
    with engine.begin() as conn:
        # 1) Проверим, есть ли таблица users
        users_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='users'
                ) AS exists
                """
            )
        ).scalar()

        if not users_exists:
            return  # create_all её создаст

        # 2) Добавляем колонки, если их нет
        # tg_user_id
        conn.execute(
            text(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS tg_user_id BIGINT
                """
            )
        )

        # ✅ ВАЖНО: привести tg_user_id к BIGINT даже если колонка была создана как INT
        conn.execute(
            text(
                """
                ALTER TABLE users
                ALTER COLUMN tg_user_id TYPE BIGINT
                USING tg_user_id::BIGINT
                """
            )
        )

        # ✅ ВАЖНО: привести users.id к BIGINT (иначе tg_id 5e9 не влезет)
        # Это критично, если ты используешь стратегию id=tg_user_id.
        conn.execute(
            text(
                """
                ALTER TABLE users
                ALTER COLUMN id TYPE BIGINT
                USING id::BIGINT
                """
            )
        )

        # bank
        conn.execute(
            text(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS bank DOUBLE PRECISION NOT NULL DEFAULT 0.0
                """
            )
        )

        # username/first_name/last_name
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT"""))

        # premium fields
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP NULL"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_live_used BOOLEAN NOT NULL DEFAULT FALSE"""))

        # created_at/updated_at
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()"""))

        # 3) Индексы (idempotent)
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id)"""))
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_is_premium ON users (is_premium)"""))

        # 4) UniqueConstraint на tg_user_id (unique index)
        conn.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_user_id ON users (tg_user_id)"""))

        # 5) Подтянуть sequence users.id (важно из-за ручных id=tg_user_id)
        try:
            conn.execute(
                text(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('users','id'),
                        GREATEST((SELECT COALESCE(MAX(id), 1) FROM users), 1)
                    )
                    """
                )
            )
        except Exception as e:
            logger.warning("users.id sequence setval skipped: %s", e)


def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет.
    Плюс: bootstrap-миграции для Postgres (без Alembic), чтобы не падать на старой схеме.
    """
    try:
        # 1) создаём все таблицы, которых нет
        SQLModel.metadata.create_all(engine)

        # 2) если Postgres — догоняем схему для legacy
        if _is_postgres():
            _bootstrap_migrations_postgres()

        logger.info("DB initialized successfully.")
    except Exception as e:
        logger.exception("DB init failed (service will continue): %s", e)


@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
