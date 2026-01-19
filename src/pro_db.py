# src/pro_db.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text

from .db import SessionLocal

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dict(row: Any) -> Dict[str, Any]:
    # row может быть Row / tuple-like
    try:
        return dict(row._mapping)  # SQLAlchemy Row
    except Exception:
        try:
            return dict(row)
        except Exception:
            return {"value": row}


def is_pro(user_id: int) -> bool:
    """
    True если:
      - users.is_premium = true
      - и (premium_until is null или premium_until > now_utc)
    """
    if not user_id:
        return False

    session = SessionLocal()
    try:
        row = session.exec(
            text(
                """
                SELECT is_premium, premium_until
                FROM users
                WHERE tg_user_id = :uid OR id = :uid
                LIMIT 1
                """
            ),
            params={"uid": int(user_id)},
        ).first()

        if not row:
            return False

        d = _row_to_dict(row)
        is_premium = bool(d.get("is_premium"))
        until = d.get("premium_until")

        # premium_until может прийти строкой/naive dt — нормализуем
        if until is None:
            return is_premium

        if isinstance(until, str):
            # пробуем ISO
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except Exception:
                return is_premium  # если не распарсили — пусть is_premium решает
        elif isinstance(until, datetime):
            until_dt = until
        else:
            return is_premium

        # если naive — считаем UTC
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)

        return is_premium and until_dt > _utcnow()

    except Exception:
        logger.exception("is_pro failed")
        return False
    finally:
        session.close()


def get_pro_status(user_id: int) -> Dict[str, Any]:
    """
    Возвращает словарь статуса, удобно для админки:
    { "user_id":..., "is_premium":..., "premium_until":..., "active":... }
    """
    session = SessionLocal()
    try:
        row = session.exec(
            text(
                """
                SELECT id, tg_user_id, is_premium, premium_until
                FROM users
                WHERE tg_user_id = :uid OR id = :uid
                LIMIT 1
                """
            ),
            params={"uid": int(user_id)},
        ).first()

        if not row:
            return {"user_id": int(user_id), "exists": False, "active": False}

        d = _row_to_dict(row)
        active = is_pro(int(user_id))
        return {
            "user_id": int(user_id),
            "exists": True,
            "is_premium": bool(d.get("is_premium")),
            "premium_until": d.get("premium_until"),
            "active": bool(active),
        }
    except Exception:
        logger.exception("get_pro_status failed")
        return {"user_id": int(user_id), "exists": None, "active": False, "error": "db_error"}
    finally:
        session.close()


def grant_pro(user_id: int, days: Optional[int] = None, lifetime: bool = False) -> bool:
    """
    Выдать PRO:
      - lifetime=True => premium_until = NULL
      - days=N => premium_until = now + N days (UTC)
    Делает upsert в users.
    """
    if not user_id:
        return False

    until: Optional[datetime]
    if lifetime or days is None:
        until = None
    else:
        if int(days) <= 0:
            return False
        until = _utcnow() + timedelta(days=int(days))

    session = SessionLocal()
    try:
        # Важно: делаем upsert максимально совместимо (Postgres/SQLite).
        # Держим id=tg_user_id (как у тебя задумано), но и tg_user_id тоже заполняем.
        session.exec(
            text(
                """
                INSERT INTO users (id, tg_user_id, is_premium, premium_until, created_at, updated_at)
                VALUES (:uid, :uid, TRUE, :until, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    tg_user_id = EXCLUDED.tg_user_id,
                    is_premium = TRUE,
                    premium_until = :until,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            params={"uid": int(user_id), "until": until},
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("grant_pro failed")
        return False
    finally:
        session.close()


def revoke_pro(user_id: int) -> bool:
    """Снять PRO: is_premium=false, premium_until=null"""
    if not user_id:
        return False

    session = SessionLocal()
    try:
        session.exec(
            text(
                """
                UPDATE users
                SET is_premium = FALSE,
                    premium_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tg_user_id = :uid OR id = :uid
                """
            ),
            params={"uid": int(user_id)},
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("revoke_pro failed")
        return False
    finally:
        session.close()


# ---- aliases (на случай если ты где-то дергаешь другие имена) ----
pro_status = get_pro_status
remove_pro = revoke_pro
