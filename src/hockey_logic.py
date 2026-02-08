# src/hockey_logic.py
"""
Хоккейная «турнирная логика» (эвристика) без живой таблицы.

Задача:
- На вход: team1, team2, league (KHL/NHL/OTHER)
- На выход: спокойные и стабильные заметки (НЕ прогноз):
  * тип матча (равный / фаворит-андердог / эмоциональный),
  * уровень риска (низкий/средний/высокий),
  * фокус (PRE/LIVE/наблюдение),
  * краткие подсказки "на что смотреть".

Принцип продукта:
- не "угадываем исход", а даём контекст.
- если данных мало — говорим мягко и честно, не «unknown-тупик».

Важно:
- Это не прогноз и не рекомендация.
- Это контекст для интерпретации движения линии/рынка.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Literal, Optional

LeagueName = Literal["KHL", "NHL", "OTHER"]
Focus = Literal["PRE", "LIVE", "WATCH"]
Risk = Literal["low", "mid", "high"]
MatchType = Literal["balanced", "fav_dog", "emotional", "unknown"]

# ============================================================
# Config (optional)
# ============================================================

_CONFIG_DIR = Path(os.getenv("SPORT_CONFIG_DIR", "config")).resolve()

def _load_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # тихо — логировать пусть вызывающий код, если нужно
        return None
    return None

# optional config files:
#   config/aliases_khl.json      {"ALIAS":"CANON", ...}
#   config/tiers_khl.json        {"top":[...], "mid":[...], "bottom":[...]}
#   config/rivalries_khl.json    [["TEAM A","TEAM B"], ...]
_ALIASES_KHL: Dict[str, str] = {}
_TIERS_KHL: Dict[str, set[str]] = {"top": set(), "mid": set(), "bottom": set()}
_RIVALRIES_KHL: set[frozenset[str]] = set()

def _init_config() -> None:
    global _ALIASES_KHL, _TIERS_KHL, _RIVALRIES_KHL

    aliases = _load_json(_CONFIG_DIR / "aliases_khl.json")
    if isinstance(aliases, dict):
        _ALIASES_KHL = {str(k).strip().upper(): str(v).strip().upper() for k, v in aliases.items()}

    tiers = _load_json(_CONFIG_DIR / "tiers_khl.json")
    if isinstance(tiers, dict):
        for key in ("top", "mid", "bottom"):
            arr = tiers.get(key) or []
            if isinstance(arr, list):
                _TIERS_KHL[key] = {str(x).strip().upper() for x in arr if str(x).strip()}

    rivals = _load_json(_CONFIG_DIR / "rivalries_khl.json")
    if isinstance(rivals, list):
        out: set[frozenset[str]] = set()
        for item in rivals:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            a = _normalize(str(item[0]))
            b = _normalize(str(item[1]))
            if a and b:
                out.add(frozenset((a, b)))
        _RIVALRIES_KHL = out

_init_config()

# ============================================================
# Built-in fallback tiers (если config не задан)
# ============================================================

# Эвристика tiers для КХЛ (можно расширять через config/tiers_khl.json)
KHL_TOP_TEAMS_FALLBACK = {
    "СКА",
    "ЦСКА",
    "АК БАРС",
    "АВАНГАРД",
    "ДИНАМО МСК",
    "ТОРПЕДО",
    "ЛОКОМОТИВ",
}
KHL_MID_TEAMS_FALLBACK = {
    "САЛАВАТ ЮЛАЕВ",
    "СПАРТАК",
    "СЕВЕРСТАЛЬ",
    "АВТОМОБИЛИСТ",
    "МЕТАЛЛУРГ",
    "ТРАКТОР",
    "ЙОКЕРИТ",
}
KHL_BOTTOM_TEAMS_FALLBACK = {
    "СОЧИ",
    "АМУР",
    "КУНЬЛУНЬ",
    "НЕФТЕХИМИК",
}

# Built-in aliases (минимум, чтобы меньше "unknown")
_ALIASES_BUILTIN = {
    "АК-БАРС": "АК БАРС",
    "АКБАРС": "АК БАРС",
    "ДИНАМО МОСКВА": "ДИНАМО МСК",
    "ДИНАМО (МОСКВА)": "ДИНАМО МСК",
    "КУНЬЛУНЬ РС": "КУНЬЛУНЬ",
    "КУНЛУНЬ": "КУНЬЛУНЬ",
    "САЛАВАТ": "САЛАВАТ ЮЛАЕВ",
}

# ============================================================
# Normalization / canonicalization
# ============================================================

_RE_SPACES = re.compile(r"\s+")
_RE_JUNK = re.compile(r"[\t\n\r\f\v]+")
_RE_PUNCT = re.compile(r"[\.,;:!?\(\)\[\]\{\}«»"'`]|\u2013|\u2014")

def _normalize(name: str) -> str:
    s = (name or "").strip().upper()
    if not s:
        return ""
    # ё -> е
    s = s.replace("Ё", "Е")
    # unify dash
    s = s.replace("-", " ")
    s = _RE_PUNCT.sub(" ", s)
    s = _RE_JUNK.sub(" ", s)
    s = _RE_SPACES.sub(" ", s).strip()
    return s

def _canonical_khl(name: str) -> str:
    n = _normalize(name)
    if not n:
        return ""
    if n in _ALIASES_KHL:
        return _ALIASES_KHL[n]
    if n in _ALIASES_BUILTIN:
        return _ALIASES_BUILTIN[n]
    return n

# ============================================================
# Core heuristics
# ============================================================

def _get_khl_tier(name: str) -> str:
    n = _canonical_khl(name)

    # first: config tiers
    if _TIERS_KHL["top"] or _TIERS_KHL["mid"] or _TIERS_KHL["bottom"]:
        if n in _TIERS_KHL["top"]:
            return "top"
        if n in _TIERS_KHL["bottom"]:
            return "bottom"
        if n in _TIERS_KHL["mid"]:
            return "mid"
        return "unknown"

    # fallback tiers
    if n in KHL_TOP_TEAMS_FALLBACK:
        return "top"
    if n in KHL_BOTTOM_TEAMS_FALLBACK:
        return "bottom"
    if n in KHL_MID_TEAMS_FALLBACK:
        return "mid"
    return "unknown"

def _label(tier: str) -> str:
    return {
        "top": "верх таблицы / претендент",
        "mid": "середина таблицы",
        "bottom": "нижняя часть таблицы",
        "unknown": "уровень не определён (без таблицы/конфига)",
    }.get(tier, "уровень не определён")

def _is_rivalry_khl(a: str, b: str) -> bool:
    if not _RIVALRIES_KHL:
        return False
    aa = _canonical_khl(a)
    bb = _canonical_khl(b)
    return frozenset((aa, bb)) in _RIVALRIES_KHL

@dataclass(frozen=True)
class MatchInsights:
    league: LeagueName
    team1: str
    team2: str
    match_type: MatchType
    risk: Risk
    focus: Focus
    confidence: Literal["high", "mid", "low"]
    notes: list[str]

def _choose_mvp_fields_khl(tier1: str, tier2: str, rivalry: bool) -> tuple[MatchType, Risk, Focus, str]:
    # confidence heuristic
    conf = "high" if ("unknown" not in {tier1, tier2}) else "low"

    if rivalry:
        return ("emotional", "high", "LIVE", conf)

    if {tier1, tier2} == {"top", "bottom"}:
        return ("fav_dog", "mid", "PRE", conf)

    if tier1 == "top" and tier2 == "top":
        return ("balanced", "mid", "LIVE", conf)

    if tier1 == "mid" and tier2 == "mid":
        return ("balanced", "mid", "LIVE", conf)

    if {"top", "mid"} == {tier1, tier2}:
        return ("fav_dog", "mid", "LIVE", conf)

    if tier1 == "bottom" and tier2 == "bottom":
        return ("unknown", "high", "WATCH", "low")

    if "unknown" in {tier1, tier2}:
        return ("unknown", "mid", "WATCH", "low")

    # default
    return ("unknown", "mid", "WATCH", conf)

def _intro_for_type(match_type: MatchType) -> str:
    return {
        "balanced": "Матч выглядит ровным — многое решит старт и первые отрезки.",
        "fav_dog": "Есть фаворит, но многое зависит от дисциплины и реализации моментов.",
        "emotional": "Матч может быть эмоциональным: выше риск хаоса и неожиданных отрезков.",
        "unknown": "Контекст ограничен: лучше опираться на составы, темп и первые минуты.",
    }[match_type]

def _focus_line(focus: Focus) -> str:
    return {
        "PRE": "Лучше смотреть PRE: составы/вратари и реакцию линии за 30–60 минут до начала.",
        "LIVE": "Лучше смотреть LIVE: по темпу и дисциплине быстро станет ясно, какой сценарий включился.",
        "WATCH": "Лучше режим наблюдения: не спешить, дождаться подтверждений по составам/темпу.",
    }[focus]

def _risk_line(risk: Risk) -> str:
    return {
        "low": "Риск: низкий (если нет сюрпризов по составам).",
        "mid": "Риск: средний (лучше без спешки).",
        "high": "Риск: высокий (легко уходит в непредсказуемость).",
    }[risk]

def build_match_insights(team1: str, team2: str, league: LeagueName = "KHL") -> MatchInsights:
    """
    Возвращает MVP-поля + готовые заметки для UI/LLM prompt.
    Совместимо с продуктовым принципом: спокойнее/понятнее/стабильнее.
    """
    t1 = (team1 or "").strip()
    t2 = (team2 or "").strip()
    if not t1 or not t2:
        return MatchInsights(league=league, team1=t1, team2=t2,
                            match_type="unknown", risk="mid", focus="WATCH",
                            confidence="low", notes=[])

    lines: list[str] = []

    # NHL-lite / OTHER: без tiers, но с полезными правилами
    if league != "KHL":
        match_type: MatchType = "unknown"
        risk: Risk = "mid"
        focus: Focus = "LIVE"
        conf: Literal["high","mid","low"] = "mid"

        lines += [
            _intro_for_type(match_type),
            _focus_line(focus),
            _risk_line(risk),
            "Что смотреть: подтверждение стартового вратаря, темп первых 10 минут, дисциплина (удаления).",
            "Когда пропустить: нет новостей по составам/вратарю или линия «плывёт» без причины.",
        ]
        return MatchInsights(league=league, team1=t1, team2=t2,
                            match_type=match_type, risk=risk, focus=focus,
                            confidence=conf, notes=[x.strip() for x in lines if x.strip()])

    # KHL
    tier1 = _get_khl_tier(t1)
    tier2 = _get_khl_tier(t2)
    rivalry = _is_rivalry_khl(t1, t2)

    match_type, risk, focus, conf = _choose_mvp_fields_khl(tier1, tier2, rivalry)

    lines.append(f"{t1}: {_label(tier1)}.")
    lines.append(f"{t2}: {_label(tier2)}.")
    if rivalry:
        lines.append("Отметка: принципиальный матч (эмоции/удаления могут повысить разброс).")

    # MVP text blocks
    lines.append(_intro_for_type(match_type))
    lines.append(_focus_line(focus))
    lines.append(_risk_line(risk))

    # Small “what to watch”
    lines.append("Что смотреть: составы/вратари, темп первых 10 минут, удаления и движение линии перед стартом.")
    lines.append("Когда пропустить: резкое движение линии без новостей, неожиданные перестановки в составе/в воротах.")

    # Universal мысль (позиционирование)
    lines.append("Ключевая мысль: сильные команды распределяют усилия по календарю — это меняет темп и риск-профиль.")

    return MatchInsights(
        league=league,
        team1=t1,
        team2=t2,
        match_type=match_type,
        risk=risk,
        focus=focus,
        confidence=conf,  # type: ignore[arg-type]
        notes=[x.strip() for x in lines if str(x).strip()],
    )

def build_match_context_notes(team1: str, team2: str, league: LeagueName = "KHL") -> list[str]:
    """
    Backward-compatible: возвращает список строк (без пустых), готовых для вставки в UI/LLM prompt.
    """
    return build_match_insights(team1, team2, league).notes
