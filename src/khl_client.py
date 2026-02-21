import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Можно переопределить через Render → ENV
FONBET_PREMATCH_URL = os.getenv(
    "FONBET_PREMATCH_URL",
    "https://line55w.bk6bba-resources.com/events/list.json",
)


@dataclass
class Outcome:
    name: str
    price: float


@dataclass
class Market:
    name: str
    outcomes: List[Outcome] = field(default_factory=list)


@dataclass
class KhlEvent:
    id: int
    team1: str
    team2: str
    start_time: Optional[datetime] = None
    markets: List[Market] = field(default_factory=list)


# ---------- DEMO-МАТЧ (СКА — ЦСКА) ----------


def _build_demo_event_ska_cska() -> KhlEvent:
    market = Market(
        name="1X2",
        outcomes=[
            Outcome(name="1", price=1.85),
            Outcome(name="X", price=3.90),
            Outcome(name="2", price=2.10),
        ],
    )
    return KhlEvent(
        id=123456,
        team1="СКА",
        team2="ЦСКА",
        start_time=None,
        markets=[market],
    )


def build_khl_today_matches_demo() -> str:
    ev = _build_demo_event_ska_cska()
    return (
        "Демо-режим КХЛ (пока без живых коэффициентов):\n\n"
        f"{ev.id}: {ev.team1} — {ev.team2}\n"
        "Линия 1X2:\n"
        "• 1: 1.85\n"
        "• X: 3.90\n"
        "• 2: 2.10\n\n"
        "Для разбора линии напиши: 'анализ матча 123456'."
    )


def get_demo_event(event_id: int) -> Optional[KhlEvent]:
    if event_id == 123456:
        return _build_demo_event_ska_cska()
    return None


# ---------- РЕАЛЬНЫЕ ДАННЫЕ FONBET ----------


async def get_today_khl_events() -> List[KhlEvent]:
    """
    Тянем prematch-лист Fonbet и выдёргиваем:
    - только хоккей (sportId == 2)
    - только турниры с 'КХЛ' в названии чемпионата
    - только матчи на сегодня (по дате UTC)
    - пытаемся собрать рынок 1X2 из customFactors (t=1,2,3)
    """
    url = FONBET_PREMATCH_URL
    if not url:
        logger.warning("FONBET_PREMATCH_URL не задан, возвращаю пустой список матчей КХЛ.")
        return []

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    events_data = data.get("events", [])
    champs = {c.get("id"): c for c in data.get("champs", [])}
    teams = {t.get("id"): t for t in data.get("teams", [])}
    custom_factors = data.get("customFactors", [])

    # индексируем коэффициенты по id события
    cf_by_event: Dict[int, List[Dict[str, Any]]] = {}
    for cf in custom_factors:
        e_id = cf.get("e")
        if isinstance(e_id, int):
            cf_by_event.setdefault(e_id, []).append(cf)

    today = datetime.now(timezone.utc).date()
    result: List[KhlEvent] = []

    for ev in events_data:
        try:
            # только хоккей
            if ev.get("sportId") != 2:
                continue

            champ = champs.get(ev.get("championshipId"))
            if not champ:
                continue
            champ_name = (champ.get("name") or "").upper()
            if "КХЛ" not in champ_name:
                continue

            start_raw = ev.get("startTime")
            if start_raw is None:
                continue

            # Fonbet обычно даёт unixtime в секундах
            dt = datetime.fromtimestamp(start_raw, tz=timezone.utc)
            if dt.date() != today:
                continue

            team1_name = (teams.get(ev.get("team1Id")) or {}).get("name") or "Команда 1"
            team2_name = (teams.get(ev.get("team2Id")) or {}).get("name") or "Команда 2"

            # Пытаемся собрать 1X2 из customFactors: t=1,2,3 → 1,X,2
            cf_list = cf_by_event.get(ev.get("id"), [])
            outcomes: List[Outcome] = []
            for t_id, label in ((1, "1"), (2, "X"), (3, "2")):
                price = None
                for cf in cf_list:
                    if cf.get("t") == t_id:
                        price = cf.get("v")
                        break
                if price is not None:
                    try:
                        outcomes.append(Outcome(name=label, price=float(price)))
                    except (TypeError, ValueError):
                        continue

            markets: List[Market] = []
            if outcomes:
                markets.append(Market(name="1X2", outcomes=outcomes))

            result.append(
                KhlEvent(
                    id=ev["id"],
                    team1=team1_name,
                    team2=team2_name,
                    start_time=dt,
                    markets=markets,
                )
            )
        except Exception:
            logger.exception("Ошибка при разборе события Fonbet: %r", ev)

    return result
