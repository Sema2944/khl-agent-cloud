# src/khl_client.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx


@dataclass
class BetLine:
    id: str              # внутренний id линии/матча
    league: str
    home: str
    away: str
    start: datetime
    market: str          # например "1X2"
    bookmaker: str
    odds_home: float
    odds_away: float
    odds_draw: Optional[float] = None
    model_prob_home: Optional[float] = None
    model_prob_away: Optional[float] = None
    model_prob_draw: Optional[float] = None
    edge_home: Optional[float] = None
    edge_away: Optional[float] = None
    edge_draw: Optional[float] = None


# ==== ВСПОМОГАТЕЛЬНОЕ: парсер Winline (каркас) ====


WINLINE_API_URL = "https://www.winline.ru/api/v2/line?sport=khl"  # ПРИМЕР! подгонять под реальный URL


async def _fetch_winline_json() -> dict:
    """
    Тянем JSON с сервера Winline.
    Здесь URL пока примерный — его нужно потом подогнать под реальный эндпоинт.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(WINLINE_API_URL)
        resp.raise_for_status()
        return resp.json()


def _parse_winline_json(data: dict) -> List[BetLine]:
    """
    Парсим "сырой" JSON Winline в список BetLine.
    Структуру нужно будет подогнать под реальное API.
    Сейчас — разумный каркас.
    """
    lines: List[BetLine] = []

    # >>> НИЖЕ ПРИМЕР СТРУКТУРЫ. ПОДГОНИМ ПОТОМ ПОД РЕАЛЬНЫЙ JSON <<<
    # Допустим, data["events"] — список матчей
    events = data.get("events") or data.get("matches") or []

    for ev in events:
        try:
            league = ev.get("league", "KHL")
            home = ev.get("homeTeam", {}).get("name") or ev.get("home", "Home")
            away = ev.get("awayTeam", {}).get("name") or ev.get("away", "Away")

            # время начала — часто в формате ISO или timestamp
            # пробуем ISO:
            start_raw = ev.get("startTime") or ev.get("start")
            if isinstance(start_raw, str):
                # простой парсер ISO (2025-11-13T17:30:00Z и т.п.)
                start = datetime.fromisoformat(
                    start_raw.replace("Z", "+00:00")
                )
            elif isinstance(start_raw, (int, float)):
                start = datetime.fromtimestamp(start_raw, tz=timezone.utc)
            else:
                start = datetime.now(tz=timezone.utc)

            # коэффициенты. Предположим, есть поле markets -> list
            odds_home = 0.0
            odds_away = 0.0
            odds_draw: Optional[float] = None
            market_name = "1X2"

            markets = ev.get("markets", [])
            for m in markets:
                name = m.get("name", "").lower()
                # ищем 1X2
                if "1x2" in name or "исход" in name:
                    outcomes = m.get("outcomes", [])
                    for out in outcomes:
                        code = out.get("code") or out.get("name", "").upper()
                        k = float(out.get("price") or out.get("k") or 0)
                        if code in ("1", "HOME"):
                            odds_home = k
                        elif code in ("2", "AWAY"):
                            odds_away = k
                        elif code in ("X", "DRAW"):
                            odds_draw = k
                    break

            # если не нашли рынок — пропускаем матч
            if not odds_home or not odds_away:
                continue

            line = BetLine(
                id=str(ev.get("id") or ev.get("eventId") or f"{home}-{away}-{start.timestamp()}"),
                league=league,
                home=home,
                away=away,
                start=start,
                market=market_name,
                bookmaker="Winline",
                odds_home=odds_home,
                odds_away=odds_away,
                odds_draw=odds_draw,
                # пока модельные вероятности и edge — None
            )
            lines.append(line)

        except Exception:
            # лучше не падать на одном битом событии
            continue

    return lines


async def get_today_lines() -> List[BetLine]:
    """
    Основная точка входа для бота: вернуть список линий на сегодня.
    Сейчас — Winline + простая фильтрация по дате.
    """
    try:
        raw = await _fetch_winline_json()
    except Exception:
        # если Winline недоступен — можно вернуть пустой список или демо
        return []

    all_lines = _parse_winline_json(raw)

    # Фильтруем по "сегодня" (UTC) грубо: дата совпадает
    today = datetime.now(tz=timezone.utc).date()
    today_lines = [l for l in all_lines if l.start.date() == today]

    return today_lines
