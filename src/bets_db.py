# src/bets_db.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from sqlmodel import SQLModel, Field, Session, select
from sqlalchemy import BigInteger


# ---------- МОДЕЛИ В БД ----------

class User(SQLModel, table=True):
    """
    Пользователь бота.

    id — это тот же user_id, который приходит из Telegram/клиента.
    ВАЖНО: Telegram user_id может быть > 2^31-1 => нужен BIGINT.
    """
    __tablename__ = "users"

    id: int = Field(primary_key=True, index=True, sa_type=BigInteger)
    bank: Optional[float] = Field(default=None)


class Bet(SQLModel, table=True):
    """
    Запись о ставке пользователя.
    """
    __tablename__ = "bets"

    id: Optional[int] = Field(default=None, primary_key=True)

    # ВАЖНО: тоже BIGINT, потому что хранит Telegram user_id
    user_id: int = Field(index=True, sa_type=BigInteger)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    raw_text: str

    stake: Optional[float] = None
    odds: Optional[float] = None
    event: Optional[str] = None
    outcome: Optional[str] = None

    result: Optional[str] = Field(default=None, index=True)  # "win" / "lose" / "push" / None

    profit: Optional[float] = None
    settled_at: Optional[datetime] = None


# ---------- ВСПОМОГАТЕЛЬНЫЕ СТРУКТУРЫ ----------

@dataclass
class UserStats:
    total_bets: int
    settled_bets: int
    pushes: int
    winrate: float
    roi: float
    pnl: float
    total_stake: float


# ---------- ОПЕРАЦИИ С ПОЛЬЗОВАТЕЛЕМ / БАНКОМ ----------

def get_or_create_user(session: Session, user_id: int) -> User:
    user_id = int(user_id)
    statement = select(User).where(User.id == user_id)
    user: User | None = session.exec(statement).one_or_none()

    if user is None:
        user = User(id=user_id, bank=None)
        session.add(user)
        session.commit()
        session.refresh(user)

    return user


def get_user_bank(session: Session, user_id: int) -> Optional[float]:
    user = get_or_create_user(session, user_id)
    return user.bank


def set_user_bank(session: Session, user_id: int, bank: float) -> User:
    user = get_or_create_user(session, user_id)
    user.bank = float(bank)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def change_user_bank(session: Session, user_id: int, delta: float) -> User:
    user = get_or_create_user(session, user_id)
    current = float(user.bank or 0.0)
    new_value = current + float(delta)
    if new_value < 0:
        new_value = 0.0
    user.bank = new_value
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------- ОПЕРАЦИИ СО СТАВКАМИ ----------

def add_bet(
    session: Session,
    user_id: int,
    raw_text: str,
    stake: Optional[float] = None,
    odds: Optional[float] = None,
    event: Optional[str] = None,
    outcome: Optional[str] = None,
) -> Bet:
    user_id = int(user_id)
    get_or_create_user(session, user_id)

    bet = Bet(
        user_id=user_id,
        raw_text=raw_text,
        stake=stake,
        odds=odds,
        event=event,
        outcome=outcome,
    )
    session.add(bet)
    session.commit()
    session.refresh(bet)
    return bet


def get_last_bets(session: Session, user_id: int, limit: int = 5) -> List[Bet]:
    user_id = int(user_id)
    statement = (
        select(Bet)
        .where(Bet.user_id == user_id)
        .order_by(Bet.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(statement))


def get_all_bets(session: Session, user_id: int) -> List[Bet]:
    user_id = int(user_id)
    statement = select(Bet).where(Bet.user_id == user_id)
    return list(session.exec(statement))


def settle_bet(session: Session, user_id: int, bet_id: int, result: str) -> Optional[Bet]:
    user_id = int(user_id)
    bet_id = int(bet_id)

    result = (result or "").lower().strip()
    if result in ("win", "выигрыш", "выиграл", "выиграла", "выиграли"):
        norm_result = "win"
    elif result in ("lose", "loss", "проигрыш", "проиграл", "проиграла", "проиграли"):
        norm_result = "lose"
    elif result in ("push", "refund", "возврат", "возврату", "возврата"):
        norm_result = "push"
    else:
        return None

    statement = select(Bet).where(Bet.id == bet_id, Bet.user_id == user_id)
    bet = session.exec(statement).one_or_none()
    if bet is None:
        return None

    bet.result = norm_result
    bet.settled_at = datetime.utcnow()

    if bet.stake is not None:
        stake = float(bet.stake)
        odds = float(bet.odds) if bet.odds is not None else None

        if norm_result == "win":
            if odds is not None and odds > 1.01:
                bet.profit = stake * (odds - 1.0)
            else:
                bet.profit = stake
        elif norm_result == "lose":
            bet.profit = -stake
        else:
            bet.profit = 0.0

    session.add(bet)
    session.commit()
    session.refresh(bet)
    return bet


def get_user_stats(session: Session, user_id: int) -> UserStats:
    user_id = int(user_id)
    statement = select(Bet).where(Bet.user_id == user_id)
    bets = list(session.exec(statement))

    total = len(bets)
    if total == 0:
        return UserStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0)

    wins = [b for b in bets if b.result == "win"]
    loses = [b for b in bets if b.result == "lose"]
    non_push = wins + loses
    pushes = [b for b in bets if b.result == "push"]

    settled_count = len(non_push)
    if settled_count == 0:
        return UserStats(total, 0, len(pushes), 0.0, 0.0, 0.0, 0.0)

    winrate = len(wins) / settled_count * 100.0
    pnl = sum(b.profit or 0.0 for b in non_push)
    total_stake = sum(b.stake or 0.0 for b in non_push)
    roi = (pnl / total_stake * 100.0) if total_stake > 0 else 0.0

    return UserStats(
        total_bets=total,
        settled_bets=settled_count,
        pushes=len(pushes),
        winrate=winrate,
        roi=roi,
        pnl=pnl,
        total_stake=total_stake,
    )
