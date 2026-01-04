# src/db.py
from __future__ import annotations

import logging
import os
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


def _col_exists(conn, table: str, col: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name=:t
                      AND column_name=:c
                )
                """
            ),
            {"t": table, "c": col},
        ).scalar()
    )


def _bootstrap_migrations_postgres() -> None:
    """
    Мини-миграции без Alembic (MVP):
    - добавляем недостающие колонки в users
    - приводим типы к BIGINT там, где нужны Telegram ID
    - приводим usage_counters.user_id к BIGINT
    - добавляем индексы
    - фиксируем sequence users.id после ручных id (id=tg_user_id)
    """
    with engine.begin() as conn:
        # ---------- users ----------
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

        # tg_user_id
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_user_id BIGINT"""))

        # ВАЖНО: после ADD COLUMN — приводим тип (если legacy был INTEGER)
        # NULL-safe: NULL::BIGINT ок
        conn.execute(
            text(
                """
                ALTER TABLE users
                ALTER COLUMN tg_user_id TYPE BIGINT
                USING tg_user_id::BIGINT
                """
            )
        )

        # users.id тоже должен быть BIGINT (ты используешь id=tg_user_id)
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

        # индексы users
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id)"""))
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_is_premium ON users (is_premium)"""))
        conn.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_user_id ON users (tg_user_id)"""))

        # ---------- bets ----------
        bets_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='bets'
                ) AS exists
                """
            )
        ).scalar()

        if bets_exists and _col_exists(conn, "bets", "user_id"):
            conn.execute(
                text(
                    """
                    ALTER TABLE bets
                    ALTER COLUMN user_id TYPE BIGINT
                    USING user_id::BIGINT
                    """
                )
            )
            conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_bets_user_id ON bets (user_id)"""))

        # ---------- usage_counters ----------
        usage_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='usage_counters'
                ) AS exists
                """
            )
        ).scalar()

        if usage_exists and _col_exists(conn, "usage_counters", "user_id"):
            conn.execute(
                text(
                    """
                    ALTER TABLE usage_counters
                    ALTER COLUMN user_id TYPE BIGINT
                    USING user_id::BIGINT
                    """
                )
            )
            conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_usage_counters_user_id ON usage_counters (user_id)"""))

        # ---------- sequence users.id ----------
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
    Плюс: делает bootstrap-миграции для Postgres (без Alembic).
    """
    try:
        SQLModel.metadata.create_all(engine)

        if _is_postgres():
            _bootstrap_migrations_postgres()

        logger.info("DB initialized successfully.")
    except Exception as e:
        logger.exception("DB init failed (service will continue): %s", e)


# ✅ ВАЖНО: НЕ @contextmanager.
# FastAPI Depends и твой parsing.py ожидают generator (yield), а не GeneratorContextManager.
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
