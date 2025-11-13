# src/khl_client.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

# TODO: здесь импортируешь свой реальный код парсинга / модели
# from khl_agent import some_module


@dataclass
class BetLine:
    league: str          # например: "KHL"
    home: str            # хозяева
    away: str            # гости
    start: datetime      # время начала матча
    market: str          # рынок, например: "1X2", "Победа с ОТ"
    bookmaker: str       # название конторы/источника
    odds_home: float
    odds_away: float
    odds_draw: Optional[float] = None
    model_prob_home: Optional[float] = None
    model_prob_away: Optional[float] = None
    model_prob_draw: Optional[float] = None
    edge_home: Optional[float] = None   # value (например, 0.05 = +5%)
    edge_away: Optional[float] = None
    edge_draw: Optional[float] = None


async def get_today_lines() -> List[BetLine]:
    """
    Вернуть список линий на сегодня.
    Сейчас — заглушка, чтобы бот уже умел что-то показать.
    Потом сюда подключишь реальный парсер/модель.
    """
    now = datetime.utcnow()
    example = BetLine(
        league="KHL",
        home="СКА",
        away="ЦСКА",
        start=now,
        market="1X2",
        bookmaker="DemoBook",
        odds_home=1.85,
        odds_away=2.10,
        odds_draw=3.90,
        model_prob_home=0.54,
        model_prob_away=0.38,
        model_prob_draw=0.08,
        edge_home=0.04,   # типа +4% value на хозяев
        edge_away=None,
        edge_draw=None,
    )
    return [example]   # вернули один матч как пример

