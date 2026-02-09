# src/parsing.py
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

from src.integrations.sport_api import SportAPIClient

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _today_msk() -> str:
    return datetime.now(MSK).date().isoformat()


def _sport_emoji(sport: str) -> str:
    return {
        "ice-hockey": "🏒",
        "football": "⚽",
        "basketball": "🏀",
        "tennis": "🎾",
        "table-tennis": "🏓",
        "esports": "🎮",
    }.get(sport, "🏟")


def _parse_sports_list(raw: str) -> List[str]:
    """Parse sports list from user text. Supports ru/en aliases."""
    raw = (raw or "").lower()

    alias_map = {
        "ice-hockey": {"ice-hockey", "hockey", "хоккей"},
        "football": {"football", "soccer", "футбол"},
        "basketball": {"basketball", "баскетбол"},
        "tennis": {"tennis", "теннис"},
        "table-tennis": {"table-tennis", "tabletennis", "tt", "настоль", "пинг"},
        "esports": {"esports", "cyber", "кибер", "киберспорт"},
    }

    picked: List[str] = []
    for canon, aliases in alias_map.items():
        if any(a in raw for a in aliases):
            picked.append(canon)

    # default: hockey + football
    if not picked:
        picked = ["ice-hockey", "football"]

    # de-dup preserve order
    out: List[str] = []
    seen = set()
    for s in picked:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _what_to_watch_simple(sport: str) -> List[str]:
    base = [
        "составы/травмы перед стартом",
        "кто начинает (основа/ключевые игроки)",
        "как двигается линия перед матчем",
    ]
    if sport == "ice-hockey":
        return base + ["темп и удаления в 1-м периоде"]
    if sport == "football":
        return base + ["погода и стартовые схемы"]
    return base


def _m_team_a(m: Dict[str, Any]) -> str:
    return (
        m.get("team_a")
        or m.get("home")
        or m.get("home_team")
        or m.get("team_home")
        or "Команда A"
    )


def _m_team_b(m: Dict[str, Any]) -> str:
    return (
        m.get("team_b")
        or m.get("away")
        or m.get("away_team")
        or m.get("team_away")
        or "Команда B"
    )


def _m_league(m: Dict[str, Any]) -> str:
    return m.get("league") or m.get("tournament") or m.get("competition") or ""


def _m_when(m: Dict[str, Any], fallback: str) -> str:
    return str(m.get("start_time") or m.get("time") or m.get("date") or fallback)


def _m_status(m: Dict[str, Any]) -> str:
    return str(m.get("status") or "").upper()


def _m_score(m: Dict[str, Any]) -> str:
    # разные API могут отдавать score по-разному
    s = m.get("score")
    if isinstance(s, str) and s.strip():
        return s.strip()
    if isinstance(s, dict):
        a = s.get("a") or s.get("home") or s.get("team_a")
        b = s.get("b") or s.get("away") or s.get("team_b")
        if a is not None and b is not None:
            return f"{a}:{b}"
    a = m.get("score_a") or m.get("home_score")
    b = m.get("score_b") or m.get("away_score")
    if a is not None and b is not None:
        return f"{a}:{b}"
    return ""


async def _safe_match_by_id(api: SportAPIClient, sport: str, match_id: int) -> Optional[Dict[str, Any]]:
    """Try several method names for compatibility."""
    for name in ("match_by_id", "get_match", "get_match_by_id"):
        fn = getattr(api, name, None)
        if fn is None:
            continue
        try:
            res = await fn(sport, match_id)
            if isinstance(res, dict):
                return res
        except TypeError:
            # some clients may not require sport
            try:
                res = await fn(match_id)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass
        except Exception:
            logger.exception("match_by_id failed (%s) sport=%s id=%s", name, sport, match_id)
    return None


async def build_daily_pro_digest(user_id: int, sports: Optional[List[str]] = None) -> str:
    sports = sports or ["ice-hockey", "football"]
    today = _today_msk()
    api = SportAPIClient()

    picks: List[Dict[str, Any]] = []

    for sport in sports:
        try:
            matches = await api.matches_by_date(sport, today)
        except Exception:
            logger.exception("DAILY PRO: matches_by_date failed sport=%s", sport)
            matches = []

        def is_live_or_upcoming(m: Dict[str, Any]) -> bool:
            st = _m_status(m)
            return not ("FIN" in st or "CANC" in st or "POST" in st)

        primary = [m for m in matches if is_live_or_upcoming(m)] or matches

        def key(m: Dict[str, Any]) -> str:
            return str(m.get("start_time") or m.get("time") or m.get("date") or m.get("id") or m.get("match_id") or "")

        primary.sort(key=key)

        for m in primary[:2]:
            picks.append({"sport": sport, "m": m})

    picks = picks[:3]

    sport_badges = " ".join(_sport_emoji(s) for s in sports)
    out: List[str] = []
    out.append(f"🧠 DAILY PRO | {sport_badges}")
    out.append(f"📅 {today}")
    out.append("")
    out.append("🔥 Топ-3 события дня (что интересно посмотреть)")
    out.append("")

    if not picks:
        out.append("Сегодня по выбранным видам спорта матчей не нашлось.")
        out.append("")
        return "\n".join(out).strip()

    for i, item in enumerate(picks, 1):
        sport = item["sport"]
        m = item["m"]
        a, b = _m_team_a(m), _m_team_b(m)
        league = _m_league(m)
        when = _m_when(m, today)

        out.append(f"{i}) {_sport_emoji(sport)} {a} — {b}")
        out.append(f"   {league} | {when}".strip())
        out.append("   Что проверить перед матчем:")
        for x in _what_to_watch_simple(sport):
            out.append(f"   • {x}")
        out.append("")

    out.append("🧩 Простая идея (без навязывания)")
    out.append("• Выбери 1–2 матча и просто следи за тем, что меняется по составам и линии перед стартом.")
    out.append("")
    out.append("⛔ Когда лучше пропустить")
    out.append("• нет подтверждений по составам")
    out.append("• странные движения линии без новостей")
    out.append("• мало информации по матчу")
    out.append("")
    out.append("ℹ️ Это аналитика для наблюдения, не рекомендация.")
    return "\n".join(out).strip()


async def format_matches_today(sport: str) -> str:
    today = _today_msk()
    api = SportAPIClient()
    try:
        matches = await api.matches_by_date(sport, today)
    except Exception:
        logger.exception("matches_by_date failed sport=%s", sport)
        return "Не удалось получить матчи на сегодня. Попробуй позже."

    if not matches:
        return f"Сегодня нет матчей по {_sport_emoji(sport)} {sport}."

    lines: List[str] = [f"{_sport_emoji(sport)} Матчи сегодня • {today}", ""]
    for i, m in enumerate(matches[:20], 1):
        mid = m.get("id") or m.get("match_id") or ""
        a, b = _m_team_a(m), _m_team_b(m)
        when = _m_when(m, today)
        st = _m_status(m)
        sc = _m_score(m)
        tail = f" | {sc}" if sc else ""
        lines.append(f"{i}) {a} — {b} ({when}) [{st}]{tail} • id={mid}")
    if len(matches) > 20:
        lines.append("")
        lines.append(f"Показано 20 из {len(matches)}. Напиши: матч <id>")
    return "\n".join(lines).strip()


async def format_match(sport: str, match_id: int) -> str:
    api = SportAPIClient()
    m = await _safe_match_by_id(api, sport, match_id)
    if not m:
        return "Матч не найден. Проверь id и попробуй ещё раз."

    a, b = _m_team_a(m), _m_team_b(m)
    league = _m_league(m)
    when = _m_when(m, _today_msk())
    st = _m_status(m)
    sc = _m_score(m)

    lines = [
        f"{_sport_emoji(sport)} {a} — {b}",
        f"{league}".strip(),
        f"🕒 {when}",
        f"Статус: {st}" + (f" | Счёт: {sc}" if sc else ""),
        "",
        "Доступно: PRE / LIVE / LIVE PRO (через кнопки в боте)",
    ]
    return "\n".join([x for x in lines if x]).strip()


async def format_ui_match_ai(sport: str, match_id: int, mode: str) -> str:
    """AI-текст для PRE/LIVE/LIVE PRO. Без советов ставить, только обзор."""
    api = SportAPIClient()
    m = await _safe_match_by_id(api, sport, match_id)

    title = f"{_sport_emoji(sport)} {mode.upper()}-обзор"
    if not m:
        return f"{title}\n\nНе смог загрузить детали матча прямо сейчас. Попробуй обновить."

    a, b = _m_team_a(m), _m_team_b(m)
    league = _m_league(m)
    when = _m_when(m, _today_msk())
    st = _m_status(m)
    sc = _m_score(m)

    lines: List[str] = [title, "", f"{a} — {b}", f"{league} | {when}".strip(), f"Статус: {st}" + (f" | {sc}" if sc else ""), ""]

    if mode.lower() == "pre":
        lines.append("Что важно проверить до стартового свистка:")
        for x in _what_to_watch_simple(sport):
            lines.append(f"• {x}")
        lines.append("")
        lines.append("На что обратить внимание по ходу матча:")
        lines.append("• первые 10–15 минут: темп и инициатива")
        lines.append("• неожиданные изменения состава/вратаря/схемы")
    elif mode.lower() == "live":
        lines.append("На что смотреть прямо сейчас:")
        lines.append("• темп (много моментов или вязкая игра)")
        lines.append("• удаления/карточки, травмы, замены")
        lines.append("• как меняется рисунок после гола/шайбы")
    else:  # live pro
        lines.append("LIVE PRO (чуть глубже, но простыми словами):")
        lines.append("• сравни ожидаемый темп с реальным (моменты/броски/удары)")
        lines.append("• кто доминирует по отрезкам и почему (смены, прессинг, спецбригады)")
        lines.append("• если игра \"сломалась\" (удаление/красная/травма) — переоценка сценария")

    lines.append("")
    lines.append("ℹ️ Это обзор для понимания игры, не рекомендация.")
    return "\n".join(lines).strip()


async def run_dialog_agent(user_id: int, text: str) -> str:
    """
    Локальный "AI-агент" (без внешних LLM).
    Нужен, чтобы бот был стабильным: всегда возвращает понятный текст.
    """
    t = _normalize_text(text)

    # DAILY PRO digest (AI)
    if "охотник дня" in t or t.startswith("daily pro") or re.search(r"\bdaily\s*pro\b", t):
        sports = _parse_sports_list(t)
        return await build_daily_pro_digest(user_id, sports)

    if t.startswith("ping"):
        return "pong ✅"

    if t.startswith("стратегия") or t.startswith("профиль"):
        return (
            "Профиль/стратегия:\n"
            "• Я показываю матчи и делаю короткий обзор (PRE/LIVE), без призывов ставить.\n"
            "• Можно выбрать вид спорта: хоккей/футбол и т.д.\n"
        )

    m = re.match(r"матчи сегодня(?:\s+([a-z-]+))?$", t)
    if m:
        sport = (m.group(1) or "ice-hockey").strip()
        return await format_matches_today(sport)

    m = re.match(r"матч\s+(\d+)$", t)
    if m:
        match_id = int(m.group(1))
        # если спорт не указан — пробуем хоккей, потом футбол
        for sport in ("ice-hockey", "football"):
            txt = await format_match(sport, match_id)
            if "не найден" not in txt.lower():
                return txt
        return "Матч не найден. Проверь id и попробуй ещё раз."

    m = re.match(r"ui match\s+(\d+)\s+([a-z-]+)\s+(pre|live|pro|livepro)$", t)
    if m:
        match_id = int(m.group(1))
        sport = m.group(2).strip()
        mode = m.group(3)
        mode_norm = "live pro" if mode in ("pro", "livepro") else mode
        return await format_ui_match_ai(sport, match_id, mode_norm)

    return (
        "Не понял команду.\n\n"
        "Доступно:\n"
        "• ping\n"
        "• матчи сегодня [ice-hockey|football|basketball|tennis|table-tennis|esports]\n"
        "• матч <match_id>\n"
        "• daily pro [хоккей/футбол/...]\n"
    )
