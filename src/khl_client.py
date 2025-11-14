# src/khl_client.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
import logging

import httpx

logger = logging.getLogger(__name__)


# ======================= МОДЕЛЬ ЛИНИИ =======================

@dataclass
class BetLine:
    league: str          # например: "KHL"
    home: str            # хозяева
    away: str            # гости
    start: datetime      # время начала матча (UTC)
    market: str          # рынок, например: "1X2"
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


# ======================= КОНСТАНТЫ WINLINE =======================

WINLINE_HOCKEY_URL = "https://winline.ru/stavki/sport/xokkej"

# Заголовки, чтобы нас не посчитали странным ботом/скриптом
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ======================= ВНЕШНЯЯ ФУНКЦИЯ =======================

async def get_today_lines() -> List[BetLine]:
    """
    Главная функция, которую вызывает бот.

    1. Пытается скачать HTML с Winline (страница хоккея).
    2. Пытается распарсить актуальные линии.
    3. Если что-то идёт не так — отдаёт демо-линию,
       чтобы бот не падал и продолжал работать.
    """
    try:
        html = await _fetch_winline_html()
    except Exception as e:
        logger.exception("Не удалось скачать страницу Winline: %s", e)
        return _demo_lines()

    try:
        lines = _parse_winline_html(html)
    except Exception as e:
        logger.exception("Ошибка парсинга HTML Winline: %s", e)
        return _demo_lines()

    if not lines:
        # Если парсер ничего не нашёл — тоже не валимся, а даём демо
        logger.warning("Парсер Winline вернул пустой список — использую демо-линию.")
        return _demo_lines()

    return lines


# ======================= HTTP-КЛИЕНТ =======================

async def _fetch_winline_html() -> str:
    """
    Скачиваем HTML страницы хоккея с Winline.

    В будущем, если найдём JSON-API Winline для prematch,
    можно будет вместо HTML дёргать чистый JSON.
    """
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=15.0) as client:
        resp = await client.get(WINLINE_HOCKEY_URL)
        resp.raise_for_status()
        return resp.text


# ======================= ПАРСИНГ HTML =======================

def _parse_winline_html(html: str) -> List[BetLine]:
    """
    Черновой парсер HTML Winline.

    ВАЖНО:
    - Сейчас мы не видим весь JS/JSON Winline (они подгружают много через XHR),
      у нас только статический HTML-пример.
    - Поэтому тут пока «каркас» парсера и fallback.
    - Как только будет известен реальный JSON/структура,
      внутрь этой функции можно будет вставить нормальный разбор.
    """

    # TODO: когда появится реальный JSON/HTML-структура:
    #  1. Найти в HTML <script> с JSON-состоянием (Nuxt/React/Angular).
    #  2. Вытянуть оттуда список событий (KHL / хоккей).
    #  3. Преобразовать в список BetLine.

    # Пока — просто возвращаем ту же демо-линию, но помечаем,
    # что источник якобы Winline, чтобы было видно, откуда это.
    logger.info("Пока что парсер Winline работает в демо-режиме.")
    return _demo_lines(bookmaker="Winline (demo parser)")


# ======================= DEMO-ДАННЫЕ =======================

def _demo_lines(bookmaker: str = "DemoBook") -> List[BetLine]:
    """
    Запасной вариант: одна демо-игра КХЛ, чтобы бот не молчал.
    """
    now = datetime.now(timezone.utc)

    example = BetLine(
        league="KHL",
        home="СКА",
        away="ЦСКА",
        start=now,
        market="1X2",
        bookmaker=bookmaker,
        odds_home=1.85,
        odds_away=2.10,
        odds_draw=3.90,
        model_prob_home=0.54,
        model_prob_away=0.38,
        model_prob_draw=0.08,
        edge_home=0.04,   # условные +4% value на хозяев
        edge_away=None,
        edge_draw=None,
    )
    return [example]
