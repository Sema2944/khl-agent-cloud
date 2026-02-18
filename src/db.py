# src/db.py
from __future__ import annotations

import logging
import os
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session  # <-- важно: sqlmodel.Session

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

# ✅ ВАЖНО: делаем фабрику с class_=sqlmodel.Session, чтобы был .exec()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=Session)


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


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema='public' AND table_name=:t
                ) AS exists
                """
            ),
            {"t": table},
        ).scalar()
    )


def _bootstrap_migrations_postgres() -> None:
    """
    Мини-миграции без Alembic (MVP):
    - users: tg_user_id BIGINT, id BIGINT + индексы
    - bets.user_id BIGINT
    - usage_counters.user_id BIGINT
    - чинит users_id_seq (если он был создан как INTEGER)
    - фиксирует setval users_id_seq (если sequence вообще используется)
    """
    with engine.begin() as conn:
        # ---------- users ----------
        if not _table_exists(conn, "users"):
            return  # create_all её создаст

        # tg_user_id
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_user_id BIGINT"""))
        conn.execute(
            text(
                """
                ALTER TABLE users
                ALTER COLUMN tg_user_id TYPE BIGINT
                USING tg_user_id::BIGINT
                """
            )
        )

        # users.id -> BIGINT (важно для id=tg_user_id)
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

        # P0: onboarding + hunter trial
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS user_seen_intro BOOLEAN NOT NULL DEFAULT FALSE"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP NULL"""))

        # P0: daily_picks table (Hunter)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS daily_picks (
                id SERIAL PRIMARY KEY,
                pick_date DATE NOT NULL,
                match_id TEXT NOT NULL DEFAULT '',
                sport_slug TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                league TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                analysis_text TEXT NOT NULL DEFAULT '',
                pick_type TEXT NOT NULL DEFAULT 'top3',
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_daily_picks_date ON daily_picks (pick_date)"""))

        # created_at/updated_at
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()"""))
        conn.execute(text("""ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()"""))

        # индексы users
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id)"""))
        conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_users_is_premium ON users (is_premium)"""))
        conn.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_user_id ON users (tg_user_id)"""))

        # ---------- bets ----------
        if _table_exists(conn, "bets") and _col_exists(conn, "bets", "user_id"):
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
        if _table_exists(conn, "usage_counters") and _col_exists(conn, "usage_counters", "user_id"):
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

        # ---------- users_id_seq: сделать BIGINT (если существует) ----------
        # Если sequence остался integer — setval будет падать при MAX(id) > 2^31-1
        try:
            seq = conn.execute(text("""SELECT pg_get_serial_sequence('users','id')""")).scalar()
            if seq:
                conn.execute(text(f"""ALTER SEQUENCE {seq} AS BIGINT"""))
                # на всякий случай максималку тоже "в 64-bit"
                conn.execute(text(f"""ALTER SEQUENCE {seq} MAXVALUE 9223372036854775807"""))

                # ---------- setval ----------
                conn.execute(
                    text(
                        f"""
                        SELECT setval(
                            '{seq}',
                            GREATEST((SELECT COALESCE(MAX(id), 1) FROM users), 1)
                        )
                        """
                    )
                )
        except Exception as e:
            logger.warning("users.id sequence fix skipped: %s", e)


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


# ✅ FastAPI Depends ожидает generator (yield)
def get_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
