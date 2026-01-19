# src/pro_db.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

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


# ============================================================
# CORE: PRO status
# ============================================================
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

        if until is None:
            return is_premium

        # premium_until может прийти строкой/naive dt — нормализуем
        if isinstance(until, str):
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except Exception:
                return is_premium
        elif isinstance(until, datetime):
            until_dt = until
        else:
            return is_premium

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
    Возвращает словарь статуса:
    { "user_id":..., "is_premium":..., "premium_until":..., "active":... }
    """
    session = SessionLocal()
    try:
        row = session.exec(
            text(
                """
                SELECT id, tg_user_id, is_premium, premium_until, trial_live_used
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
            "trial_live_used": bool(d.get("trial_live_used") or False),
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
        session.exec(
            text(
                """
                INSERT INTO users (id, tg_user_id, is_premium, premium_until, trial_live_used, created_at, updated_at)
                VALUES (:uid, :uid, TRUE, :until, COALESCE(:trial_used, FALSE), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    tg_user_id = EXCLUDED.tg_user_id,
                    is_premium = TRUE,
                    premium_until = :until,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            params={"uid": int(user_id), "until": until, "trial_used": False},
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


# ============================================================
# TRIAL: LIVE PRO (1 раз бесплатно)
# ============================================================
def ensure_user_row(user_id: int) -> None:
    """
    Гарантирует, что в users есть запись для пользователя.
    Это нужно, чтобы мы могли отметить trial_live_used даже у "нового" юзера.
    """
    if not user_id:
        return

    session = SessionLocal()
    try:
        # Вставляем минимально, если нет. Если есть — просто обновим updated_at.
        session.exec(
            text(
                """
                INSERT INTO users (id, tg_user_id, is_premium, premium_until, trial_live_used, created_at, updated_at)
                VALUES (:uid, :uid, FALSE, NULL, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    tg_user_id = EXCLUDED.tg_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            params={"uid": int(user_id)},
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("ensure_user_row failed")
    finally:
        session.close()


def get_user_flags(user_id: int) -> Dict[str, Any]:
    """
    Возвращает флаги (trial и т.п.). Если записи нет — вернёт дефолты.
    """
    if not user_id:
        return {"trial_live_used": False}

    session = SessionLocal()
    try:
        row = session.exec(
            text(
                """
                SELECT trial_live_used
                FROM users
                WHERE tg_user_id = :uid OR id = :uid
                LIMIT 1
                """
            ),
            params={"uid": int(user_id)},
        ).first()

        if not row:
            return {"trial_live_used": False}

        d = _row_to_dict(row)
        return {"trial_live_used": bool(d.get("trial_live_used") or False)}
    except Exception:
        logger.exception("get_user_flags failed")
        return {"trial_live_used": False, "error": "db_error"}
    finally:
        session.close()


def consume_live_trial(user_id: int) -> bool:
    """
    Помечает trial_live_used=True. Возвращает True если успешно.
    """
    if not user_id:
        return False

    ensure_user_row(user_id)

    session = SessionLocal()
    try:
        session.exec(
            text(
                """
                UPDATE users
                SET trial_live_used = TRUE,
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
        logger.exception("consume_live_trial failed")
        return False
    finally:
        session.close()


def can_access_live_pro(user_id: int) -> Tuple[bool, bool]:
    """
    Возвращает (allowed, use_trial_now)

    - allowed=True если:
        * пользователь PRO
        * или trial ещё не использован (тогда use_trial_now=True)
    - allowed=False если:
        * не PRO и trial уже потрачен
    """
    if not user_id:
        return False, False

    if is_pro(user_id):
        return True, False

    flags = get_user_flags(user_id)
    used = bool(flags.get("trial_live_used") or False)
    if used:
        return False, False

    return True, True


# ---- aliases (на случай если где-то дергаешь другие имена) ----
pro_status = get_pro_status
remove_pro = revoke_pro
