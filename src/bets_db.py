# src/bets_db.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from sqlmodel import SQLModel, Field, Session, select


# ---------- МОДЕЛИ В БД ----------


class Bet(SQLModel, table=True):
    """
    Запись о ставке пользователя.
    """
    __tablename__ = "bets"

    id: Optional[int] = Field(default=None, primary_key=True)

    user_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # сырое описание ставки, как ввёл пользователь
    raw_text: str

    # базовые поля
    stake: Optional[float] = None          # сумма ставки
    odds: Optional[float] = None           # коэффициент (например 1.85)
    event: Optional[str] = None            # матч / событие (пока не парсим)
    outcome: Optional[str] = None          # исход (П1, тотал и т.п.)

    # результат
    result: Optional[str] = Field(
        default=None, index=True
    )  # "win" / "lose" / "push" / None

    # финансы
    profit: Optional[float] = None         # + / - в тех же единицах, что stake
    settled_at: Optional[datetime] = None  # когда ставка была рассчитана


# ---------- ВСПОМОГАТЕЛЬНЫЕ СТРУКТУРЫ ----------


@dataclass
class UserStats:
    total_bets: int        # всего ставок (в т.ч. нерасчитанных)
    settled_bets: int      # рассчитанных (win/lose, без возвратов)
    pushes: int            # количество возвратов
    winrate: float         # % выигрышей по win/lose
    roi: float             # ROI по win/lose
    pnl: float             # общий плюс/минус (win/lose)
    total_stake: float     # суммарный объём ставок (win/lose)


# ---------- ОПЕРАЦИИ С БАЗОЙ ----------


def add_bet(
    session: Session,
    user_id: int,
    raw_text: str,
    stake: Optional[float] = None,
    odds: Optional[float] = None,
    event: Optional[str] = None,
    outcome: Optional[str] = None,
) -> Bet:
    """
    Сохраняем ставку в БД.
    """
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
    """
    Возвращаем последние N ставок пользователя.
    """
    statement = (
        select(Bet)
        .where(Bet.user_id == user_id)
        .order_by(Bet.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(statement))


def settle_bet(session: Session, user_id: int, bet_id: int, result: str) -> Optional[Bet]:
    """
    Отмечаем ставку рассчитанной: result = "win" / "lose" / "push".

    Расчёт PnL:
    - если есть stake и odds:
        win  -> profit = stake * (odds - 1)
        lose -> profit = -stake
        push -> profit = 0
    - если есть только stake:
        win  -> profit = +stake
        lose -> profit = -stake
        push -> profit = 0
    """
    result = result.lower().strip()
    if result in ("win", "выигрыш", "выиграл", "выиграла", "выиграли"):
        norm_result = "win"
    elif result in ("lose", "loss", "проигрыш", "проиграл", "проиграла", "проиграли"):
        norm_result = "lose"
    elif result in ("push", "refund", "возврат", "возврату", "возврата"):
        norm_result = "push"
    else:
        # неизвестный результат
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
                # классика: чистая прибыль = ставка * (кэф - 1)
                bet.profit = stake * (odds - 1.0)
            else:
                # если кэф неизвестен, считаем прибыль = ставка
                bet.profit = stake
        elif norm_result == "lose":
            bet.profit = -stake
        elif norm_result == "push":
            bet.profit = 0.0

    session.add(bet)
    session.commit()
    session.refresh(bet)
    return bet


def get_user_stats(session: Session, user_id: int) -> UserStats:
    """
    Реальная статистика по пользователю.

    Винрейт и ROI считаем только по win/lose (без push),
    но при этом учитываем количество возвратов отдельно.
    """
    statement = select(Bet).where(Bet.user_id == user_id)
    bets = list(session.exec(statement))

    total = len(bets)
    if total == 0:
        return UserStats(
            total_bets=0,
            settled_bets=0,
            pushes=0,
            winrate=0.0,
            roi=0.0,
            pnl=0.0,
            total_stake=0.0,
        )

    wins = [b for b in bets if b.result == "win"]
    loses = [b for b in bets if b.result == "lose"]
    non_push = wins + loses
    pushes = [b for b in bets if b.result == "push"]

    settled_count = len(non_push)

    if settled_count == 0:
        return UserStats(
            total_bets=total,
            settled_bets=0,
            pushes=len(pushes),
            winrate=0.0,
            roi=0.0,
            pnl=0.0,
            total_stake=0.0,
        )

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
