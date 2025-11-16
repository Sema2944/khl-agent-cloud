# src/bets_db.py
from typing import Optional, List
from datetime import datetime

from sqlmodel import SQLModel, Field, Session, select


class Bet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int

    event_id: Optional[int] = None
    sport: Optional[str] = None
    league: Optional[str] = None
    team1: Optional[str] = None
    team2: Optional[str] = None
    market: Optional[str] = None

    odds: float
    stake: float
    status: str = "open"  # open | win | lose | push
    profit: float = 0.0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    settled_at: Optional[datetime] = None


class UserStats(SQLModel):
    total_bets: int
    wins: int
    losses: int
    pushes: int
    pnl: float
    roi: float
    winrate: float


def get_user_bets(session: Session, user_id: int, limit: int = 1000) -> List[Bet]:
    stmt = (
        select(Bet)
        .where(Bet.user_id == user_id)
        .order_by(Bet.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt))


def get_user_stats(session: Session, user_id: int) -> UserStats:
    bets = get_user_bets(session, user_id)
    total = len(bets)

    if total == 0:
        return UserStats(
            total_bets=0,
            wins=0,
            losses=0,
            pushes=0,
            pnl=0.0,
            roi=0.0,
            winrate=0.0,
        )

    wins = sum(1 for b in bets if b.status == "win")
    losses = sum(1 for b in bets if b.status == "lose")
    pushes = sum(1 for b in bets if b.status == "push")
    pnl = sum(b.profit for b in bets)
    invested = sum(b.stake for b in bets)
    roi = (pnl / invested * 100) if invested else 0.0
    winrate = wins / total * 100

    return UserStats(
        total_bets=total,
        wins=wins,
        losses=losses,
        pushes=pushes,
        pnl=pnl,
        roi=roi,
        winrate=winrate,
    )

