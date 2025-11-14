# src/khl_client.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Optional

import logging

# Если решишь реально парсить Winline — будешь использовать httpx/BeautifulSoup и т.п.
# import httpx
# from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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


# ====================== Winline: КАРКАС ======================

async def _fetch_winline_today_raw() -> List[BetLine]:
    """
    Здесь ДОЛЖЕН быть реальный парсер Winline.

    Сейчас это заглушка, чтобы не ломать логику.
    Как только у тебя будет конкретный HTML/JSON от Winline, можно будет:
      - сделать http-запрос (httpx)
      - распарсить события
      - заполнить объекты BetLine
    """
    # TODO: Реализовать реальный парсер Winline.
    # Пример структуры того, что нужно вернуть — смотри demo-линии ниже.
    return []


# ====================== ПУБЛИЧНЫЙ API ДЛЯ БОТА ======================

async def get_today_lines() -> List[BetLine]:
    """
    Вернуть список линий на сегодня.
    1) Пытаемся взять реальные линии из Winline.
    2) Если не получилось / пусто — возвращаем демо-пример.
    """
    try:
        lines = await _fetch_winline_today_raw()
        if lines:
            # Можно ещё отфильтровать по дате (только матчи на сегодня)
            logger.info("get_today_lines: получено %d линий из Winline", len(lines))
            return lines
        else:
            logger.warning("get_today_lines: Winline вернул пустой список, используем демо.")
    except Exception as e:
        logger.exception("Ошибка при работе с Winline, используем демо: %s", e)

    # ------- DEMO: один матч СКА — ЦСКА -------
    now = datetime.utcnow()
    today = date.today()

    demo = BetLine(
        league="KHL",
        home="СКА",
        away="ЦСКА",
        start=datetime(
            year=today.year,
            month=today.month,
            day=today.day,
            hour=17,
            minute=0,
        ),
        market="1X2",
        bookmaker="Demo Winline",
        odds_home=1.85,
        odds_away=2.10,
        odds_draw=3.90,
        model_prob_home=0.54,
        model_prob_away=0.38,
        model_prob_draw=0.08,
        edge_home=0.04,   # +4% value на хозяев
        edge_away=None,
        edge_draw=None,
    )
    return [demo]
