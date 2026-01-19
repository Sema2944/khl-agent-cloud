# src/pro_db.py
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional, Tuple

from sqlalchemy import text
from sqlmodel import Session

from .db import get_session

logger = logging.getLogger(__name__)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    gen = get_session()
    session = next(gen)
    try:
        yield session
    finally:
        try:
            gen.close()
        except Exception:
            pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_pro(user_id: int) -> bool:
    """
    PRO = users.is_premium == true AND (premium_until is NULL OR premium_until > now)
    Поддерживаем оба варианта идентификации: users.id==user_id или users.tg_user_id==user_id
    """
    if not user_id:
        return False

    try:
        with db_session() as session:
            row = session.exec(
                text(
                    """
                    SELECT is_premium, premium_until
                    FROM users
                    WHERE id = :uid OR tg_user_id = :uid
                    LIMIT 1
                    """
                ),
                {"uid": int(user_id)},
            ).first()

            if not row:
                return False

            is_premium = bool(row[0])
            premium_until = row[1]  # может быть None или datetime

            if not is_premium:
                return False

            if premium_until is None:
                return True

            # сравниваем в UTC (если naive — считаем UTC)
            now = _utcnow()
            if isinstance(premium_until, datetime) and premium_until.tzinfo is None:
                premium_until = premium_until.replace(tzinfo=timezone.utc)

            return bool(premium_until > now)
    except Exception:
        logger.exception("is_pro failed")
        return False


def grant_pro(user_id: int, *, days: Optional[int] = None) -> Tuple[bool, str]:
    """
    Выдать PRO: is_premium=true, premium_until = now+days (или NULL если lifetime)
    Если записи users нет — пытаемся вставить минимальную запись.
    """
    if not user_id:
        return False, "user_id пустой"

    until: Optional[datetime]
    if days is None:
        until = None
    else:
        until = _utcnow() + timedelta(days=int(days))

    try:
        with db_session() as session:
            now = _utcnow()
            res = session.exec(
                text(
                    """
                    UPDATE users
                    SET is_premium = TRUE,
                        premium_until = :until,
                        updated_at = :now
                    WHERE id = :uid OR tg_user_id = :uid
                    """
                ),
                {"uid": int(user_id), "until": until, "now": now},
            )
            session.commit()

            # если не обновилось — пробуем INSERT
            if getattr(res, "rowcount", 0) == 0:
                session.exec(
                    text(
                        """
                        INSERT INTO users (id, tg_user_id, is_premium, premium_until, trial_live_used, bank, created_at, updated_at)
                        VALUES (:uid, :uid, TRUE, :until, FALSE, 0.0, :now, :now)
                        """
                    ),
                    {"uid": int(user_id), "until": until, "now": now},
                )
                session.commit()

        return True, "✅ PRO выдан"
    except Exception as e:
        logger.exception("grant_pro failed")
        return False, f"Ошибка выдачи PRO: {e}"


def revoke_pro(user_id: int) -> Tuple[bool, str]:
    if not user_id:
        return False, "user_id пустой"

    try:
        with db_session() as session:
            now = _utcnow()
            session.exec(
                text(
                    """
                    UPDATE users
                    SET is_premium = FALSE,
                        premium_until = NULL,
                        updated_at = :now
                    WHERE id = :uid OR tg_user_id = :uid
                    """
                ),
                {"uid": int(user_id), "now": now},
            )
            session.commit()
        return True, "✅ PRO отключён"
    except Exception as e:
        logger.exception("revoke_pro failed")
        return False, f"Ошибка отключения PRO: {e}"
