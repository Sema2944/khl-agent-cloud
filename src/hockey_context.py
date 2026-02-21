# src/hockey_context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


# --------- Простые справочники (эвристика) ---------
KHL_TOP_TEAMS = {
    "СКА",
    "ЦСКА",
    "АК БАРС",
    "АВАНГАРД",
    "ДИНАМО МСК",
    "ЛОКОМОТИВ",
    "ТОРПЕДО",
}

KHL_MID_TEAMS = {
    "САЛАВАТ ЮЛАЕВ",
    "СПАРТАК",
    "СЕВЕРСТАЛЬ",
    "АВТОМОБИЛИСТ",
    "МЕТАЛЛУРГ",
    "ТРАКТОР",
    "ЙОКЕРИТ",
}

KHL_BOTTOM_TEAMS = {
    "СОЧИ",
    "АМУР",
    "КУНЬЛУНЬ",
    "НЕФТЕХИМИК",
}


@dataclass(frozen=True)
class TeamContext:
    name: str
    tier: str
    label: str


def _normalize(name: str) -> str:
    return (name or "").strip().upper()


def _tier_label(tier: str) -> str:
    return {
        "top": "верх таблицы / претендент",
        "mid": "середина таблицы",
        "bottom": "нижняя часть таблицы",
        "unknown": "статус не определён (без таблицы)",
    }.get(tier, "статус не определён")


def get_khl_tier(team_name: str) -> str:
    name = _normalize(team_name)
    if name in KHL_TOP_TEAMS:
        return "top"
    if name in KHL_BOTTOM_TEAMS:
        return "bottom"
    if name in KHL_MID_TEAMS:
        return "mid"
    return "unknown"


def build_team_context(team_name: str) -> TeamContext:
    tier = get_khl_tier(team_name)
    return TeamContext(name=(team_name or "").strip(), tier=tier, label=_tier_label(tier))


def build_match_context(team1: str, team2: str) -> List[Dict[str, str]]:
    t1 = build_team_context(team1)
    t2 = build_team_context(team2)
    return [
        {"team": t1.name, "tier": t1.tier, "label": t1.label},
        {"team": t2.name, "tier": t2.tier, "label": t2.label},
    ]
