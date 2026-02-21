from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Sport = Literal["hockey", "football", "basketball", "mma"]


@dataclass(frozen=True)
class Match:
    id: str
    sport: Sport
    league: str
    start_time_iso: Optional[str]
    home: str
    away: str


@dataclass(frozen=True)
class Market:
    """
    Нормализованный рынок. ЭТО и будет входом в LLM.

    type:
      - moneyline (1X2 / ML)
      - total
      - handicap
    """
    type: Literal["moneyline", "total", "handicap"]
    # общий value (для total/handicap): например 5.5 или -1.5
    value: Optional[float] = None

    # moneyline: home/draw/away
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None

    # total: over/under
    over: Optional[float] = None
    under: Optional[float] = None

    # handicap: home/away
    home_handicap: Optional[float] = None
    away_handicap: Optional[float] = None


@dataclass(frozen=True)
class Line:
    match: Match
    markets: list[Market]
    source: str  # например "oddsapi", "somebook", "demo"
    fetched_at_iso: str
