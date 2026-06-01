# src/daily_pro.py
"""
Daily Hunter v2.1: AI-powered top picks generator.
Pipeline: fetch → filter top leagues → deterministic score → enrich odds →
          team context (standings+form) → AI analysis (top-10 only) →
          value filter (edge ≥ 3%) → diversify → save to DB → broadcast.
Runs at 08:00 UTC via scheduler in service.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, date
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session

from .db import engine
from .integrations.sport_api import SportAPIClient
from .llm_client import analyze_with_llm_cached
from .pro_db import OWNER_IDS

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

# Channel auto-posting (set CHANNEL_USERNAME env var or leave empty to disable)
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "").strip()

# ---------------------------------------------------------------------------
# Hunter run status (module-level, survives across calls)
# ---------------------------------------------------------------------------
_hunter_run_info: Dict[str, Any] = {
    "last_run_at": None,       # datetime (MSK)
    "picks_count": 0,
    "users_sent": 0,
    "users_total": 0,
    "status": "never_run",     # "never_run" | "running" | "done" | "error"
    "error": None,
}

HUNTER_SPORTS = ["ice-hockey", "football", "basketball", "tennis", "mma"]

# ---------------------------------------------------------------------------
# TOP LEAGUES — only matches from these leagues are eligible for Hunter
# ---------------------------------------------------------------------------
TOP_LEAGUES_KEYWORDS: Dict[str, List[str]] = {
    "football": [
        "premier league", "epl", "la liga", "serie a", "bundesliga",
        "ligue 1", "рпл", "rpl", "российская премьер",
        "champions league", "лига чемпионов",
        "europa league", "лига европы",
        "conference league",
        # Летние лиги (hotfix 31.05.2026 — пока топ-лиги Европы в паузе)
        "allsvenskan", "veikkausliiga", "eliteserien", "segunda división", "segunda division",
    ],
    "ice-hockey": [
        "khl", "кхл", "nhl", "нхл", "shl", "liiga",
        # Кубок Гагарина / плей-офф КХЛ
        "gagarin", "кубок гагарина", "playoff", "play-off", "плей-офф",
    ],
    "basketball": [
        "nba", "нба", "euroleague", "евролига",
        # Единая Лига ВТБ
        "vtb", "втб", "единая лига", "united league",
    ],
    "tennis": [
        "atp", "wta", "grand slam", "australian open",
        "roland garros", "french open", "wimbledon", "us open",
        # ATP Masters 1000
        "masters", "atp 500", "atp 1000",
    ],
    "mma": [
        "ufc",
    ],
}

SPORT_EMOJI = {
    "football": "⚽", "ice-hockey": "🏒", "basketball": "🏀",
    "tennis": "🎾", "mma": "🥊",
}


def _is_top_league(sport: str, league: str) -> bool:
    """Check if league name matches any top-league keyword for the sport."""
    lg = (league or "").lower()
    keywords = TOP_LEAGUES_KEYWORDS.get(sport, [])
    return any(kw in lg for kw in keywords)


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _truncate_at_sentence(text: str, limit: int = 200) -> str:
    """Truncate text at the last complete sentence within limit."""
    if not text or len(text) <= limit:
        return text
    chunk = text[:limit]
    # Find last sentence-ending punctuation
    for sep in [". ", "! ", "? "]:
        idx = chunk.rfind(sep)
        if idx > 0:
            return chunk[:idx + 1]
    # No sentence break — try just a period at end
    idx = chunk.rfind(".")
    if idx > limit // 2:
        return chunk[:idx + 1]
    return chunk.rstrip() + "…"


def _extract_hhmm(start_time: str) -> str:
    """Extract 'HH:MM' from any time format: '19:30', '2026-02-22T19:30:00+03:00', etc."""
    if not start_time:
        return ""
    m = re.search(r"(\d{1,2}):(\d{2})", str(start_time))
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def _extract_hour_msk(start_time: str) -> Optional[int]:
    """Extract hour (MSK) from start_time string."""
    hhmm = _extract_hhmm(start_time)
    if hhmm:
        return int(hhmm.split(":")[0])
    return None


def _extract_moneyline(odds_raw: Any) -> Dict[str, Any]:
    """Extract home/draw/away odds from various API response formats."""
    if not odds_raw or not isinstance(odds_raw, dict):
        return {}

    result: Dict[str, Any] = {}

    # Format 1: {"home": 1.65, "draw": 3.80, "away": 5.20}
    for key_h in ("home", "1", "homeWin", "home_win"):
        v = _safe_float(odds_raw.get(key_h))
        if v > 1.0:
            result["home"] = round(v, 2)
            break

    for key_d in ("draw", "x", "X", "tie"):
        v = _safe_float(odds_raw.get(key_d))
        if v > 1.0:
            result["draw"] = round(v, 2)
            break

    for key_a in ("away", "2", "awayWin", "away_win"):
        v = _safe_float(odds_raw.get(key_a))
        if v > 1.0:
            result["away"] = round(v, 2)
            break

    # Format 2: nested {"moneyline": {"home": ..., "away": ...}}
    if not result:
        ml = odds_raw.get("moneyline") or odds_raw.get("1x2") or {}
        if isinstance(ml, dict):
            for key_h in ("home", "1"):
                v = _safe_float(ml.get(key_h))
                if v > 1.0:
                    result["home"] = round(v, 2)
                    break
            for key_d in ("draw", "x"):
                v = _safe_float(ml.get(key_d))
                if v > 1.0:
                    result["draw"] = round(v, 2)
                    break
            for key_a in ("away", "2"):
                v = _safe_float(ml.get(key_a))
                if v > 1.0:
                    result["away"] = round(v, 2)
                    break

    # Totals
    for key_t in ("total", "totals", "overUnder"):
        t = odds_raw.get(key_t)
        if isinstance(t, dict):
            line = _safe_float(t.get("line") or t.get("total") or t.get("value"))
            over = _safe_float(t.get("over"))
            under = _safe_float(t.get("under"))
            if line > 0 and over > 1.0:
                result["total_line"] = line
                result["total_over"] = round(over, 2)
                result["total_under"] = round(under, 2) if under > 1.0 else 0
            break

    return result


# ---------------------------------------------------------------------------
# AI PROMPT — concrete recommendation with odds
# ---------------------------------------------------------------------------
_HUNTER_ANALYSIS_PROMPT = """Ты — AI-аналитик спортивных событий для бота Betly.
Проанализируй матч и дай КОНКРЕТНУЮ рекомендацию.

Матч: {title}
Лига: {league}
Страна: {country}
Время: {start_time}
Коэффициенты: {odds_text}
{context_text}{line_movement_text}
ЗАДАЧА:
1. Дай одну КОНКРЕТНУЮ рекомендацию (П1, П2, X, ТБ N, ТМ N, или комбинацию)
2. Объясни в 2-3 предложениях ПОЧЕМУ — {analysis_basis}
{context_instruction}
4. Если есть движение линии — учти его в анализе
5. Оцени уверенность от 55% до 85%

Верни ТОЛЬКО JSON (без markdown, без ```):
{{
  "confidence": число от 0.55 до 0.85,
  "recommendation": "П1" или "П2" или "ТБ 2.5" или "П1 + ТБ 2.5" (краткая формулировка),
  "rec_odds": число (коэффициент на рекомендацию из данных выше, например 1.75),
  "summary": "2-3 предложения анализа с фактами"
}}

КРИТИЧЕСКИ ВАЖНО:
- НЕ придумывай команды, форму, статистику или коэффициенты — используй ТОЛЬКО данные выше.
- Если коэффициентов нет ("нет данных") — rec_odds = 0, recommendation ТОЛЬКО на основе позиций команд.
- Если данных о командах недостаточно для анализа — верни confidence=0.55, recommendation="" (пустая строка).
- Пустая recommendation лучше выдуманной рекомендации.
- {context_bias_guard}
- Без слов "ставь", "бери", "гарантия". Только аналитический материал.
- Отвечай на русском."""


# ---------------------------------------------------------------------------
# Team context: standings + form (cached per league/day)
# ---------------------------------------------------------------------------
_standings_cache: Dict[str, Any] = {}  # key: "sport:league_id:season"


def _extract_team_names(title: str):
    """Split 'Team A — Team B' → ('Team A', 'Team B')."""
    parts = title.split(" — ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return title.strip(), ""


async def _fetch_team_context(match: Dict[str, Any]) -> str:
    """Return a short standings block for the LLM prompt.

    Calls api-sports.io /standings for the league once per day (cached).
    Extracts rank + form (last 5) for both teams.
    Returns "" silently on any error or unsupported sport.

    Example output:
        Таблица:
          #1 Arsenal | 89pt | Форма: WDWWL
          #8 Chelsea | 65pt | Форма: LWDWW
    """
    sport = match.get("sport_slug", "")
    if sport in ("tennis", "mma"):
        return ""  # no standings for these sports

    league_id = match.get("_league_id")
    season = match.get("_season")
    home_id = match.get("_home_id")
    away_id = match.get("_away_id")
    home_name, away_name = _extract_team_names(match.get("title", ""))

    if not league_id or not season:
        return ""

    cache_key = f"{sport}:{league_id}:{season}"

    if cache_key not in _standings_cache:
        try:
            from .sports_config import get_sport_config
            cfg = get_sport_config(sport) or {}
            base_url = cfg.get("api_base", "")
            api_key = os.getenv("API_SPORTS_KEY", "")
            if not base_url or not api_key:
                _standings_cache[cache_key] = None
            else:
                import httpx as _httpx
                headers = {"x-apisports-key": api_key}
                async with _httpx.AsyncClient(timeout=_httpx.Timeout(8.0)) as client:
                    r = await client.get(
                        f"{base_url}/standings",
                        params={"league": league_id, "season": season},
                        headers=headers,
                    )
                _standings_cache[cache_key] = r.json() if r.status_code == 200 else None
                logger.debug("Hunter context: standings %s → HTTP %d", cache_key, r.status_code)
        except Exception as exc:
            logger.debug("Hunter context: standings fetch error %s: %s", cache_key, exc)
            _standings_cache[cache_key] = None

    raw_data = _standings_cache.get(cache_key)
    if not raw_data:
        return ""

    # Parse standings — handle football (nested) and hockey/basketball (flat)
    teams: Dict[str, Dict[str, Any]] = {}   # name → {rank, form, points, id}
    try:
        for item in raw_data.get("response") or []:
            # Football: {"league": {"standings": [[{rank, team, points, form}]]}}
            league_obj = item.get("league") if isinstance(item, dict) else None
            groups = (league_obj or {}).get("standings") if league_obj else None
            if not groups and isinstance(item, list):
                groups = [item]  # hockey / basketball flat list
            for group in (groups or []):
                for entry in (group if isinstance(group, list) else []):
                    t = entry.get("team") or {}
                    tid = t.get("id")
                    tname = t.get("name", "")
                    info = {
                        "rank": entry.get("rank") or entry.get("position") or "?",
                        "form": entry.get("form", ""),
                        "points": entry.get("points", ""),
                        "id": tid,
                    }
                    if tname:
                        teams[tname] = info
                    if tid:
                        teams[str(tid)] = info
    except Exception:
        return ""

    if not teams:
        return ""

    def _find(name: str, tid: Any) -> Optional[Dict]:
        if tid and str(tid) in teams:
            return teams[str(tid)]
        if name in teams:
            return teams[name]
        nl = name.lower()
        for k, v in teams.items():
            if isinstance(k, str) and (nl in k.lower() or k.lower() in nl):
                return v
        return None

    home_info = _find(home_name, home_id)
    away_info = _find(away_name, away_id)
    if not home_info and not away_info:
        return ""

    def _fmt(team_name: str, info: Optional[Dict]) -> str:
        if not info:
            return ""
        rank = info.get("rank", "?")
        pts = info.get("points", "")
        form = info.get("form", "")
        pts_s = f" | {pts}pt" if pts else ""
        form_s = f" | Форма: {form}" if form else ""
        return f"  #{rank} {team_name}{pts_s}{form_s}"

    lines = ["Таблица:"]
    for name, info in [(home_name, home_info), (away_name, away_info)]:
        row = _fmt(name, info)
        if row:
            lines.append(row)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Fetch & Filter
# ---------------------------------------------------------------------------

def _is_verified_match(m: Dict[str, Any]) -> bool:
    """Return True only if the match has minimum required verified fields.

    Guards against phantom/garbage entries that arrive when an API returns
    partial data (empty title, unknown match_id, etc.).
    """
    title = (m.get("title") or "").strip()
    league = (m.get("league") or "").strip()
    match_id = (m.get("match_id") or "").strip()

    # Must look like "Team A — Team B"
    if " — " not in title:
        return False
    home, away = title.split(" — ", 1)
    if not home.strip() or not away.strip():
        return False

    # Must have a real league name
    if not league or league.lower() in ("unknown", "other", "none", ""):
        return False

    # Must have a real match_id (not placeholder)
    if not match_id or match_id.lower() in ("unknown", "none", ""):
        return False

    return True


async def _fetch_all_matches_today() -> List[Dict[str, Any]]:
    """Fetch matches across all hunter sports for today (MSK)."""
    today = datetime.now(MSK).date()
    all_matches: List[Dict[str, Any]] = []
    api = SportAPIClient()
    for sport in HUNTER_SPORTS:
        try:
            items = await api.matches_by_date(sport, today)
            sport_matches = []
            leagues_found: set = set()
            for m in items:
                # Extract league/team IDs from raw API response for standings enrichment
                raw_m = getattr(m, "raw", {}) or {}
                league_obj = raw_m.get("league") or {}
                teams_obj = raw_m.get("teams") or {}
                entry = {
                    "match_id": str(m.id),
                    "sport_slug": getattr(m, "sport_slug", sport),
                    "title": getattr(m, "title", "") or "",
                    "league": getattr(m, "league", "") or "",
                    "country": getattr(m, "country", "") or "",
                    "start_time": str(getattr(m, "start_time", "") or ""),
                    "status": str(getattr(m, "status", "") or "").lower(),
                    "odds": getattr(m, "odds_base", None),
                    # IDs for standings/context enrichment (may be None for ESPN-sourced matches)
                    "_league_id": league_obj.get("id"),
                    "_season":    league_obj.get("season"),
                    "_home_id":   (teams_obj.get("home") or {}).get("id"),
                    "_away_id":   (teams_obj.get("away") or {}).get("id"),
                    # Data quality audit trail
                    "_data_source": "api-sports.io" if league_obj.get("id") else "espn/unknown",
                }
                sport_matches.append(entry)
                leagues_found.add(entry["league"])
            all_matches.extend(sport_matches)
            logger.info(
                "Hunter fetch %s: %d matches raw, leagues: %s",
                sport, len(sport_matches), sorted(leagues_found) if leagues_found else "none",
            )
        except Exception:
            logger.exception("Hunter fetch failed for %s", sport)
    return all_matches


def _filter_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only scheduled matches from top leagues with verified minimum fields."""
    result = []
    # Diagnostic counters per sport
    sport_raw: Dict[str, int] = {}
    sport_filtered: Dict[str, int] = {}
    sport_status_skip: Dict[str, int] = {}
    unverified_skip = 0

    for m in matches:
        status = m.get("status", "")
        league = (m.get("league") or "").lower()
        sport = m.get("sport_slug", "")
        sport_raw[sport] = sport_raw.get(sport, 0) + 1

        # Only pre-match (not started)
        if status not in {"notstarted", "scheduled", "fixture", "ns", ""}:
            sport_status_skip[sport] = sport_status_skip.get(sport, 0) + 1
            continue

        # Skip friendlies, women, youth
        if any(x in league for x in ["friendly", "women", "youth", "u18", "u20", "u21"]):
            continue

        # Guardrail: skip matches without verified minimum fields
        if not _is_verified_match(m):
            unverified_skip += 1
            logger.debug("Hunter filter: unverified match dropped — title=%r league=%r id=%r",
                         m.get("title", "")[:30], m.get("league", ""), m.get("match_id", ""))
            continue

        # Top-league filter
        if not _is_top_league(sport, m.get("league", "")):
            continue

        result.append(m)
        sport_filtered[sport] = sport_filtered.get(sport, 0) + 1

    if unverified_skip:
        logger.warning("Hunter filter: dropped %d unverified matches (no title/league/teams)", unverified_skip)

    # Log diagnostics per sport
    for sport in sorted(sport_raw.keys()):
        raw_n = sport_raw.get(sport, 0)
        filt_n = sport_filtered.get(sport, 0)
        skip_n = sport_status_skip.get(sport, 0)
        logger.info(
            "Hunter filter %s: %d raw → %d filtered (%d skipped by status)",
            sport, raw_n, filt_n, skip_n,
        )

    # Fallback: if too few matches pass the top-league filter,
    # relax but KEEP the verified + no-friendly guards
    if len(result) < 6:
        logger.info("Hunter: only %d top-league matches, relaxing filter", len(result))
        relaxed = []
        for m in matches:
            status = m.get("status", "")
            league = (m.get("league") or "").lower()
            if status not in {"notstarted", "scheduled", "fixture", "ns", ""}:
                continue
            if any(x in league for x in ["friendly", "women", "youth", "u18", "u20", "u21"]):
                continue
            # Keep verified guard even in relaxed mode
            if not _is_verified_match(m):
                continue
            relaxed.append(m)
        return relaxed

    return result


# ---------------------------------------------------------------------------
# Deterministic scoring (no LLM, fast)
# ---------------------------------------------------------------------------
# Premium league keywords → extra scoring bonus
_PREMIUM_LEAGUE_KEYWORDS = [
    "champions league", "лига чемпионов",
    "europa league", "лига европы",
    "conference league",
    "nhl", "нхл",
    "nba", "нба",
    "ufc",
]

# Minor league keywords → scoring penalty
_MINOR_LEAGUE_KEYWORDS = [
    "challenger", "atp 250", "wta 250", "wta 125",
    "itf", "futures", "qualifying",
]


def _deterministic_score(match: Dict[str, Any]) -> float:
    """Score a match based on deterministic criteria (no LLM)."""
    score = 0.0
    sport = match.get("sport_slug", "")
    league = match.get("league", "")
    lg_lower = (league or "").lower()

    # 1. Top-league bonus (+30)
    if _is_top_league(sport, league):
        score += 30

    # 1b. Premium league bonus (+20): Champions League, NHL, NBA, UFC
    if any(kw in lg_lower for kw in _PREMIUM_LEAGUE_KEYWORDS):
        score += 20

    # 1c. Minor league penalty (-15): ATP 250, ITF, challengers
    if any(kw in lg_lower for kw in _MINOR_LEAGUE_KEYWORDS):
        score -= 15

    # 2. Has odds (+20)
    odds = match.get("odds")
    if odds and isinstance(odds, dict):
        score += 20

    # 3. Close odds = intrigue (+0-25)
    if odds and isinstance(odds, dict):
        oh = _safe_float(odds.get("home") or odds.get("1") or odds.get("homeWin"))
        oa = _safe_float(odds.get("away") or odds.get("2") or odds.get("awayWin"))
        if oh > 1.0 and oa > 1.0:
            diff = abs(oh - oa)
            if diff < 0.5:
                score += 25
            elif diff < 1.0:
                score += 15
            elif diff < 2.0:
                score += 10

    # 4. Evening match MSK (+5)
    hour = _extract_hour_msk(match.get("start_time", ""))
    if hour is not None and 17 <= hour <= 23:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Odds enrichment for finalists (The Odds API + fallback to sport_api)
# ---------------------------------------------------------------------------
async def _enrich_odds(match: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch detailed odds: try The Odds API first, fallback to sport_api."""
    # 1. Try The Odds API (multi-bookmaker odds)
    try:
        from .integrations.odds_api import get_match_odds as get_odds_api_odds
        title = match.get("title", "")
        parts = title.split(" — ", 1)
        if len(parts) == 2:
            odds_data = await get_odds_api_odds(
                match["sport_slug"], parts[0].strip(), parts[1].strip()
            )
            if odds_data and odds_data.get("h2h"):
                match["odds_api_data"] = odds_data
                # Extract moneyline from best odds
                best = odds_data.get("best_odds", {})
                parsed = {}
                if best.get("home", {}).get("price"):
                    parsed["home"] = round(best["home"]["price"], 2)
                if best.get("draw", {}).get("price"):
                    parsed["draw"] = round(best["draw"]["price"], 2)
                if best.get("away", {}).get("price"):
                    parsed["away"] = round(best["away"]["price"], 2)
                # Totals from first bookmaker
                totals = odds_data.get("totals", {})
                if totals:
                    first_bm = next(iter(totals.values()))
                    if first_bm.get("line") and first_bm.get("over"):
                        parsed["total_line"] = first_bm["line"]
                        parsed["total_over"] = round(first_bm["over"], 2)
                        parsed["total_under"] = round(first_bm.get("under", 0), 2)
                match["odds_parsed"] = parsed
                logger.info("Hunter: enriched %s with OddsAPI (%d bookmakers)",
                            title[:30], len(odds_data.get("h2h", {})))
                return match
    except Exception as e:
        logger.warning("Hunter: OddsAPI enrichment failed for %s: %s", match.get("title", "")[:30], e)

    # 2. Fallback: use existing odds from matches_by_date
    odds = match.get("odds")
    if odds and isinstance(odds, dict):
        match["odds_parsed"] = _extract_moneyline(odds)
        return match

    # 3. Last resort: try sport_api match_odds
    try:
        api = SportAPIClient()
        snap = await api.match_odds(match["sport_slug"], match["match_id"])
        raw = snap.raw or {}
        match["odds"] = raw
        match["odds_parsed"] = _extract_moneyline(raw)
    except Exception:
        logger.debug("Hunter: odds fetch failed for %s", match.get("match_id"))
        match["odds_parsed"] = {}
    return match


def _format_odds_text(odds: Dict[str, Any]) -> str:
    """Format odds dict to human-readable string for AI prompt."""
    parts = []
    if odds.get("home"):
        parts.append(f"П1: {odds['home']}")
    if odds.get("draw"):
        parts.append(f"X: {odds['draw']}")
    if odds.get("away"):
        parts.append(f"П2: {odds['away']}")
    if odds.get("total_line") and odds.get("total_over"):
        parts.append(f"ТБ {odds['total_line']}: {odds['total_over']}")
    return " | ".join(parts) if parts else "нет данных"


# ---------------------------------------------------------------------------
# Hunter-specific system prompt (NOT ui_live — that one overrides JSON format)
# ---------------------------------------------------------------------------
_HUNTER_SYSTEM_PROMPT = """Ты спортивный аналитик.
Отвечай СТРОГО одним JSON-объектом (без markdown, без текста, без пояснений).

Формат ответа:
{
  "confidence": число от 0.55 до 0.85,
  "recommendation": "П1" или "П2" или "ТБ 2.5" или "П1 + ТБ 2.5",
  "rec_odds": число (коэффициент),
  "summary": "2-3 предложения анализа"
}

ОБЯЗАТЕЛЬНО: recommendation НЕ МОЖЕТ быть пустым. Всегда дай конкретную рекомендацию.
Верни только JSON."""


# ---------------------------------------------------------------------------
# AI scoring with concrete recommendations
# ---------------------------------------------------------------------------
async def _ai_analyze_match(match: Dict[str, Any]) -> Dict[str, Any]:
    """Ask AI to analyze a match and give concrete recommendation.

    Uses direct LLM call with Hunter-specific system prompt
    (NOT schema="ui_live" which overrides the JSON format).
    """
    odds_parsed = match.get("odds_parsed", {})
    odds_text = _format_odds_text(odds_parsed)

    # Get line movement info (if available from odds_tracker)
    line_movement_text = ""
    try:
        from .odds_tracker import get_line_movement_summary
        title = match.get("title", "")
        parts = title.split(" — ", 1)
        home = parts[0].strip() if len(parts) == 2 else ""
        away = parts[1].strip() if len(parts) == 2 else ""
        lm = get_line_movement_summary(match["match_id"], home, away)
        if lm:
            line_movement_text = f"Движение линии:\n{lm}\n"
    except Exception:
        pass

    context_text = match.get("_context_text", "")
    if context_text:
        context_text = context_text.rstrip("\n") + "\n"
        # Context confirmed → instruct LLM to use it
        analysis_basis = "сила команд, форма, позиция в таблице, турнирная ситуация"
        context_instruction = "3. ИСПОЛЬЗУЙ позицию и последние 5 матчей из таблицы выше в анализе"
        context_bias_guard = "Используй позицию в таблице, форму команд, рейтинг для анализа."
    else:
        # No verified context → forbid hallucinating form/standings
        analysis_basis = "класс команд, их турнирная ситуация, коэффициенты"
        context_instruction = (
            "3. Данных о таблице и форме команд нет — НЕ упоминай позиции, "
            "серию матчей или форму в анализе. Опирайся только на коэффициенты "
            "и общеизвестный класс команд."
        )
        context_bias_guard = "НЕ придумывай форму, позиции в таблице или последние результаты — этих данных нет."

    prompt = _HUNTER_ANALYSIS_PROMPT.format(
        title=match.get("title", ""),
        league=match.get("league", ""),
        country=match.get("country", ""),
        start_time=match.get("start_time", ""),
        odds_text=odds_text,
        context_text=context_text,
        line_movement_text=line_movement_text,
        analysis_basis=analysis_basis,
        context_instruction=context_instruction,
        context_bias_guard=context_bias_guard,
    )

    try:
        from .llm_client import _llm_chat_json
        result = await _llm_chat_json(
            prompt,
            timeout_s=40.0,
            system_prompt=_HUNTER_SYSTEM_PROMPT,
            max_tokens=500,
        )

        logger.info("Hunter AI raw keys for %s: %s",
                     match.get("title", "")[:30], list(result.keys()) if isinstance(result, dict) else type(result).__name__)

        if isinstance(result, dict):
            # Extract fields — try multiple possible key names
            confidence = _safe_float(
                result.get("confidence")
                or result.get("conf")
                or 0.60
            )
            confidence = min(0.85, max(0.55, confidence))

            recommendation = str(
                result.get("recommendation")
                or result.get("rec")
                or result.get("pick")
                or result.get("bet")
                or ""
            ).strip()[:50]

            rec_odds = _safe_float(
                result.get("rec_odds")
                or result.get("odds")
                or 0
            )

            summary = str(
                result.get("summary")
                or result.get("analysis")
                or result.get("reasoning")
                or result.get("text")
                or ""
            ).strip()[:500]

            # Fallback: extract recommendation from summary text if empty
            if not recommendation and summary:
                recommendation = _extract_rec_from_text(summary)

            # Fallback: extract from odds if still empty
            if not recommendation:
                recommendation = _extract_rec_from_odds(odds_parsed)

            # Adjust confidence based on line movement
            try:
                from .odds_tracker import get_odds_confidence_adjustment
                adj = get_odds_confidence_adjustment(match["match_id"])
                if adj != 0:
                    confidence = min(0.85, max(0.55, confidence + adj))
                    logger.info("Hunter: confidence adjusted by %+.2f for %s",
                                adj, match.get("title", "")[:30])
            except Exception:
                pass

            logger.info("Hunter AI result: %s → conf=%.2f rec=%r odds=%.2f",
                        match.get("title", "")[:30], confidence, recommendation, rec_odds)

            return {
                **match,
                "confidence": confidence,
                "recommendation": recommendation,
                "rec_odds": rec_odds,
                "analysis_text": summary,
                "line_movement": line_movement_text,
            }

        logger.warning("Hunter AI: non-dict result for %s: %s",
                        match.get("title", "")[:30], str(result)[:200])
        return {**match, "confidence": 0.0, "analysis_text": "", "recommendation": "", "rec_odds": 0}

    except Exception:
        logger.exception("Hunter AI analysis failed for %s", match.get("match_id"))
        return {**match, "confidence": 0.0, "analysis_text": "", "recommendation": "", "rec_odds": 0}


def _extract_rec_from_text(text: str) -> str:
    """Try to extract a recommendation from analysis text."""
    t = text.upper()
    # Look for explicit recommendation patterns
    for pattern, rec in [
        ("П1 + ТБ", "П1 + ТБ 2.5"), ("П2 + ТБ", "П2 + ТБ 2.5"),
        ("П1 + ТМ", "П1 + ТМ 2.5"), ("П2 + ТМ", "П2 + ТМ 2.5"),
    ]:
        if pattern in t:
            # Try to extract the actual number
            m = re.search(rf"{re.escape(pattern)}\s*(\d+[.,]?\d*)", t)
            if m:
                return f"{pattern} {m.group(1).replace(',', '.')}"
            return rec

    # Simple patterns
    for pattern in ["ТБ", "ТМ"]:
        m = re.search(rf"\b{pattern}\s*(\d+[.,]?\d*)", t)
        if m:
            return f"{pattern} {m.group(1).replace(',', '.')}"

    if "П1" in t and "П2" not in t:
        return "П1"
    if "П2" in t and "П1" not in t:
        return "П2"
    if re.search(r"\bНИЧЬ", t) or re.match(r"^\s*X\s*$", t):
        return "X"

    return ""


def _extract_rec_from_odds(odds: Dict[str, Any]) -> str:
    """Fallback: pick favourite from odds."""
    h = _safe_float(odds.get("home"))
    a = _safe_float(odds.get("away"))
    if h > 1.0 and a > 1.0:
        return "П1" if h < a else "П2"
    return ""


# ---------------------------------------------------------------------------
# Express builder
# ---------------------------------------------------------------------------
def _build_express(top3: List[Dict[str, Any]], pick_date: date) -> Dict[str, Any]:
    """Build express pick from top-3 recommendations."""
    legs = []
    total_odds = 1.0

    for p in top3:
        rec = p.get("recommendation", "")
        odds = _safe_float(p.get("rec_odds", 0))
        # Smart title: "Chelsea — Burnley" → "Chelsea-Burnley", truncate if needed
        parts = (p.get("title") or "Матч").split(" — ")
        if len(parts) == 2:
            title_short = f"{parts[0].strip()[:12]}-{parts[1].strip()[:12]}"
        else:
            title_short = (p.get("title") or "")[:25]
        if rec:
            legs.append(f"{rec} {title_short}")
            if odds > 1.0:
                total_odds *= odds

    return {
        "match_id": f"express_{pick_date.isoformat()}",
        "sport_slug": "multi",
        "title": "Экспресс дня",
        "league": ", ".join(p.get("league", "")[:20] for p in top3),
        "confidence": min((p.get("confidence", 0) for p in top3), default=0),
        "analysis_text": " + ".join(legs),
        "recommendation": f"КЭФ {total_odds:.2f}" if total_odds > 1.0 else "",
        "start_time": "",
        "odds_parsed": {},
        "rec_odds": round(total_odds, 2) if total_odds > 1.0 else 0,
        "pick_type": "express",
    }


# ---------------------------------------------------------------------------
# Save picks to DB
# ---------------------------------------------------------------------------
def _save_picks(picks: List[Dict[str, Any]], pick_date: date) -> None:
    """Save top picks to daily_picks table (v2 with new columns)."""
    try:
        logger.info("Hunter SAVE step: _save_picks called picks=%d date=%s", len(picks), pick_date)
        with Session(engine) as s:
            logger.info("Hunter SAVE step: deleting existing daily_picks for %s", pick_date)
            s.exec(
                text("DELETE FROM daily_picks WHERE pick_date = :d"),
                params={"d": pick_date.isoformat()},
            )
            s.commit()
            logger.info("Hunter SAVE step: delete committed for %s", pick_date)

            for i, p in enumerate(picks, 1):
                odds_json_str = ""
                odds_data = p.get("odds_parsed") or {}
                if odds_data:
                    try:
                        odds_json_str = json.dumps(odds_data, ensure_ascii=False)
                    except Exception:
                        odds_json_str = ""

                logger.info(
                    "Hunter SAVE step: insert #%d type=%s match_id=%s title=%r",
                    i,
                    p.get("pick_type", "top3"),
                    p.get("match_id", ""),
                    (p.get("title", "") or "")[:60],
                )
                s.exec(
                    text("""
                        INSERT INTO daily_picks
                        (pick_date, match_id, sport_slug, title, league,
                         confidence, analysis_text, pick_type,
                         start_time, odds_json, recommendation, created_at)
                        VALUES (:d, :mid, :sport, :title, :league,
                                :conf, :txt, :ptype,
                                :stime, :ojson, :rec, NOW())
                    """),
                    params={
                        "d": pick_date.isoformat(),
                        "mid": p.get("match_id", ""),
                        "sport": p.get("sport_slug", ""),
                        "title": p.get("title", ""),
                        "league": p.get("league", ""),
                        "conf": p.get("confidence", 0.0),
                        "txt": p.get("analysis_text", ""),
                        "ptype": p.get("pick_type", "top3"),
                        "stime": p.get("start_time", ""),
                        "ojson": odds_json_str,
                        "rec": p.get("recommendation", ""),
                    },
                )
            s.commit()
            logger.info("Hunter SAVE step: insert commit ok rows=%d date=%s", len(picks), pick_date)
            logger.info("Hunter: saved %d picks for %s", len(picks), pick_date)
    except Exception:
        logger.exception("Hunter: save_picks failed")
        raise  # propagate so callers know DB write failed


# ---------------------------------------------------------------------------
# Broadcast to PRO users (v2 format)
# ---------------------------------------------------------------------------
def _format_hunter_message(picks: List[Dict[str, Any]], pick_date: date) -> str:
    """Build the Hunter broadcast message in the new rich format."""
    top3 = [p for p in picks if p.get("pick_type") == "top3"]

    lines = [
        f"🎯 Охотник — Топ матчи дня",
        f"{pick_date.strftime('%d.%m.%Y')} | Подобрано AI",
        "",
    ]

    for i, p in enumerate(top3[:3], 1):
        conf = int(float(p.get("confidence", 0)) * 100)
        sport = p.get("sport_slug", "")
        emoji = SPORT_EMOJI.get(sport, "🏆")
        title = p.get("title", "Матч")
        league = p.get("league", "")
        start = _extract_hhmm(p.get("start_time", ""))
        rec = p.get("recommendation", "")
        rec_odds = _safe_float(p.get("rec_odds", 0))
        summary = _truncate_at_sentence(p.get("analysis_text") or "", 200)

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{i}️⃣ {emoji} {title}")

        league_time = ""
        if league:
            league_time = f"   🏆 {league}"
        if start:
            league_time += f" | {start} MSK" if league_time else f"   {start} MSK"
        if league_time:
            lines.append(league_time)

        # Odds line
        odds_data = {}
        ojson = p.get("odds_json", "")
        if ojson:
            try:
                odds_data = json.loads(ojson)
            except Exception:
                pass
        if not odds_data:
            odds_data = p.get("odds_parsed", {})

        if odds_data:
            parts = []
            if odds_data.get("home"):
                parts.append(f"П1: {odds_data['home']}")
            if odds_data.get("draw"):
                parts.append(f"X: {odds_data['draw']}")
            if odds_data.get("away"):
                parts.append(f"П2: {odds_data['away']}")
            if parts:
                lines.append(f"   💰 {' | '.join(parts)}")

        # Line movement (compact, one line)
        lm = p.get("line_movement", "")
        if lm:
            # Show just the first meaningful line of movement
            lm_lines = [l.strip() for l in lm.strip().split("\n") if l.strip()]
            for ll in lm_lines:
                if ll.startswith("П1") or ll.startswith("П2") or ll.startswith("⚠️"):
                    lines.append(f"   📈 {ll}")
                    break

        # Recommendation with best bookmaker
        if rec:
            rec_str = f"   🎯 {rec}"
            if rec_odds > 1.0:
                rec_str += f" (КЭФ {rec_odds:.2f})"
            # Add best bookmaker info from OddsAPI data
            odds_api = p.get("odds_api_data", {})
            best = odds_api.get("best_odds", {}) if odds_api else {}
            # Try to match recommendation to best odds
            if best:
                rec_lower = rec.lower()
                if ("п1" in rec_lower or "home" in rec_lower) and best.get("home", {}).get("bookmaker"):
                    rec_str += f" @ {best['home']['bookmaker']}"
                elif ("п2" in rec_lower or "away" in rec_lower) and best.get("away", {}).get("bookmaker"):
                    rec_str += f" @ {best['away']['bookmaker']}"
            lines.append(rec_str)

        # Summary
        if summary:
            lines.append(f"   💡 {summary}")

        lines.append(f"   ✅ Уверенность: {conf}%")
        lines.append("")

    # Express
    express = [p for p in picks if p.get("pick_type") == "express"]
    if express:
        ep = express[0]
        express_text = ep.get("analysis_text", "")
        express_odds = ep.get("recommendation", "")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        header = "⚡ Экспресс дня"
        if express_odds:
            header += f" ({express_odds})"
        lines.append(header)
        if express_text:
            lines.append(f"  {express_text}")
        lines.append("")

    lines.append("ℹ️ Аналитический материал, не является прогнозом.")
    return "\n".join(lines)


def _format_channel_teaser(picks: List[Dict[str, Any]], pick_date: date) -> str:
    """Build teaser message for channel: matches only, no recommendations."""
    top3 = [p for p in picks if p.get("pick_type") == "top3"]

    lines = [
        f"🎯 Охотник — Топ матчи дня",
        f"{pick_date.strftime('%d.%m.%Y')} | Подобрано AI",
        "",
    ]

    for i, p in enumerate(top3[:3], 1):
        sport = p.get("sport_slug", "")
        emoji = SPORT_EMOJI.get(sport, "🏆")
        title = p.get("title", "Матч")
        league = p.get("league", "")
        start = _extract_hhmm(p.get("start_time", ""))

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{i}️⃣ {emoji} {title}")

        league_time = ""
        if league:
            league_time = f"   🏆 {league}"
        if start:
            league_time += f" | {start} MSK" if league_time else f"   {start} MSK"
        if league_time:
            lines.append(league_time)

        lines.append(f"   🔒 Прогноз — в PRO")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔒 Прогнозы и AI-анализ — в PRO подписке")
    lines.append("")
    lines.append("👉 @BetlyAIBot — открой бота")
    lines.append("🎁 Промокод BETLY2026 → 7 дней PRO бесплатно")

    return "\n".join(lines)


async def _broadcast_to_channel(bot, picks: List[Dict[str, Any]], pick_date: date) -> bool:
    """Post Hunter teaser to public Telegram channel. Returns True if sent."""
    if not CHANNEL_USERNAME:
        return False

    try:
        channel_msg = _format_channel_teaser(picks, pick_date)
        await bot.send_message(chat_id=CHANNEL_USERNAME, text=channel_msg)
        logger.info("Hunter: posted teaser to channel %s", CHANNEL_USERNAME)
        return True
    except Exception:
        logger.exception("Hunter: channel broadcast failed for %s", CHANNEL_USERNAME)
        return False


async def _broadcast_to_pro_users(bot, picks: List[Dict[str, Any]], pick_date: date) -> int:
    """Send hunter picks to all PRO users, trial users, and OWNER_IDS.

    Returns number of users successfully sent to.
    """
    if not picks:
        logger.warning("Hunter broadcast: no picks to send")
        return 0

    try:
        with Session(engine) as s:
            rows = s.exec(
                text("""
                    SELECT tg_user_id FROM users
                    WHERE tg_user_id IS NOT NULL
                      AND (
                        (is_premium = TRUE AND (premium_until IS NULL OR premium_until > NOW()))
                        OR (trial_started_at IS NOT NULL AND trial_started_at > NOW() - INTERVAL '3 days')
                      )
                """)
            ).all()
            user_ids = set(r[0] for r in rows if r[0])
    except Exception:
        logger.exception("Hunter broadcast: failed to fetch users")
        user_ids = set()

    # OWNER_IDS always receives broadcast (even if not formally PRO)
    user_ids.update(OWNER_IDS)

    if not user_ids:
        logger.warning("Hunter broadcast: no PRO users to send (OWNER_IDS also empty)")
        return 0

    logger.info("Hunter: sending broadcast to %d users (incl. %d owners)", len(user_ids), len(OWNER_IDS))

    msg = _format_hunter_message(picks, pick_date)

    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            logger.warning("Hunter broadcast failed for user %s", uid)

    logger.info("Hunter: broadcast sent to %d/%d users", sent, len(user_ids))

    # Check for expired trial users → send trial-ended message
    try:
        with Session(engine) as s:
            expired = s.exec(
                text("""
                    SELECT tg_user_id FROM users
                    WHERE trial_started_at IS NOT NULL
                      AND trial_started_at <= NOW() - INTERVAL '3 days'
                      AND trial_started_at > NOW() - INTERVAL '4 days'
                      AND is_premium = FALSE
                      AND tg_user_id IS NOT NULL
                """)
            ).all()
        for row in expired:
            uid = row[0]
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=(
                        "Твой 3-дневный пробный период Охотника завершён.\n\n"
                        "Оформи PRO, чтобы получать подборки каждый день!\n"
                        "Нажми /start → 🌟 PRO"
                    ),
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Hunter: trial expiry check failed")

    # Auto-suggest /feedback 3 days after promo activation (once per user)
    try:
        with Session(engine) as s:
            promo_3d = s.exec(
                text("""
                    SELECT pa.user_id FROM promo_activations pa
                    LEFT JOIN feedback f ON f.user_id = pa.user_id
                    WHERE pa.activated_at <= NOW() - INTERVAL '3 days'
                      AND pa.activated_at > NOW() - INTERVAL '4 days'
                      AND f.id IS NULL
                """)
            ).all()
        for row in promo_3d:
            uid = row[0]
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=(
                        "📝 Привет! Ты пользуешься Betly уже 3 дня.\n\n"
                        "Расскажи, что нравится и что улучшить — "
                        "это займёт 30 секунд:\n"
                        "/feedback\n\n"
                        "🎁 +3 дня PRO за обратную связь!"
                    ),
                )
            except Exception:
                pass
        if promo_3d:
            logger.info("Hunter: sent feedback reminder to %d promo users", len(promo_3d))
    except Exception:
        logger.exception("Hunter: feedback reminder failed")

    return sent


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------
async def run_daily_hunter(bot=None) -> list:
    """Main entry point for daily hunter job (v2).

    Returns the top3 pick list on success, [] if no picks were produced,
    or [] on error (error is logged and stored in _hunter_run_info).
    """
    logger.info("Hunter v2: starting daily run, bot=%s, channel=%s",
                "provided" if bot else "None", CHANNEL_USERNAME or "(not set)")
    _hunter_run_info["status"] = "running"
    _hunter_run_info["error"] = None
    _hunter_run_info["last_run_at"] = datetime.now(MSK)

    try:
        today = datetime.now(MSK).date()

        # 1. Fetch all matches
        matches = await _fetch_all_matches_today()
        logger.info("Hunter: fetched %d matches total", len(matches))

        # 2. Filter: only scheduled + top leagues
        filtered = _filter_matches(matches)
        logger.info("Hunter: %d matches after TOP_LEAGUES filter", len(filtered))

        # --- Diagnostic: log premium league matches (CL, EL, NHL, NBA, UFC) ---
        for m in filtered:
            lg = (m.get("league") or "").lower()
            if any(kw in lg for kw in _PREMIUM_LEAGUE_KEYWORDS):
                logger.info(
                    "Hunter PREMIUM match: %s | league=%s | sport=%s | status=%s | odds=%s",
                    m.get("title", ""), m.get("league", ""),
                    m.get("sport_slug", ""), m.get("status", ""),
                    bool(m.get("odds")),
                )

        if not filtered:
            logger.warning("Hunter: no matches found after filtering — aborting")
            _hunter_run_info["status"] = "done"
            _hunter_run_info["picks_count"] = 0
            _hunter_run_info["users_sent"] = 0
            _hunter_run_info["users_total"] = 0
            return

        # 3. Deterministic scoring (fast, no LLM)
        for m in filtered:
            m["det_score"] = _deterministic_score(m)

        filtered.sort(key=lambda x: x.get("det_score", 0), reverse=True)
        top_candidates = filtered[:10]

        # --- Diagnostic: log top-10 with sport + league + score ---
        for i, m in enumerate(top_candidates, 1):
            logger.info(
                "Hunter top-%d: score=%.0f | %s | %s | %s",
                i, m.get("det_score", 0), m.get("sport_slug", ""),
                m.get("league", "")[:40], m.get("title", "")[:50],
            )

        # 4. Enrich odds for top candidates
        for m in top_candidates:
            await _enrich_odds(m)
            await asyncio.sleep(0.3)

        # 4a. Data quality gate — require verified minimum fields after odds enrichment
        import datetime as _dt_module
        verified_at_ts = _dt_module.datetime.now(MSK).isoformat(timespec="seconds")
        dq_ok = []
        dq_dropped = []
        for m in top_candidates:
            if not _is_verified_match(m):
                dq_dropped.append(m)
                logger.warning("Hunter DQ gate: dropping %r — missing title/league/teams",
                                m.get("title", "")[:40])
                continue
            m["_verified_at"] = verified_at_ts
            m["_data_source"] = m.get("_data_source", "unknown")
            dq_ok.append(m)

        if dq_dropped:
            logger.warning("Hunter DQ gate: dropped %d/%d candidates after verified-check",
                           len(dq_dropped), len(top_candidates))
        top_candidates = dq_ok

        if not top_candidates:
            logger.warning("Hunter DQ gate: 0 verified candidates — aborting (no reliable data today)")
            _hunter_run_info["status"] = "done"
            _hunter_run_info["picks_count"] = 0
            _hunter_run_info["users_sent"] = 0
            _hunter_run_info["users_total"] = 0
            return

        # 4b. Team context: standings + form from api-sports.io
        _standings_cache.clear()  # fresh cache for today's run
        ctx_ok = 0
        for m in top_candidates:
            try:
                ctx = await _fetch_team_context(m)
                m["_context_text"] = ctx
                if ctx:
                    ctx_ok += 1
            except Exception:
                m["_context_text"] = ""
        logger.info("Hunter context: enriched %d/%d matches with standings+form",
                    ctx_ok, len(top_candidates))

        # 5. AI analysis for top candidates
        candidates_count = len(top_candidates)
        scored = []
        for m in top_candidates:
            result = await _ai_analyze_match(m)
            scored.append(result)
            await asyncio.sleep(0.5)  # rate limit

        # 6. Filter out failed analyses (confidence=0 or no recommendation)
        valid = []
        skipped_list = []
        for m in scored:
            conf = m.get("confidence", 0)
            rec = (m.get("recommendation") or "").strip()
            if conf <= 0 or not rec:
                skipped_list.append(m)
                logger.info("Hunter: AI skipped %s — confidence=%.2f rec=%r",
                            m.get("title", "")[:40], conf, rec)
                continue
            valid.append(m)
        if skipped_list:
            logger.info("Hunter: AI skipped %d matches with empty analysis", len(skipped_list))

        valid_after_ai = len(valid)

        # 6b. VALUE filter: edge = confidence − implied_probability ≥ 3%
        # Applied only when enough matches survive (otherwise results degrade).
        high_value: List[Dict[str, Any]] = []
        low_value: List[Dict[str, Any]] = []
        for m in valid:
            conf = m.get("confidence", 0.60)
            rec_odds = _safe_float(m.get("rec_odds", 0))
            if rec_odds > 1.0:
                implied = 1.0 / rec_odds
                edge = round(conf - implied, 3)
                m["_edge"] = edge
                if edge >= 0.03:
                    high_value.append(m)
                else:
                    low_value.append(m)
                    logger.info(
                        "Hunter VALUE: skipped %s — conf=%.2f odds=%.2f edge=%.1f%%",
                        m.get("title", "")[:35], conf, rec_odds, edge * 100,
                    )
            else:
                m["_edge"] = 0.0
                low_value.append(m)
                logger.info("Hunter VALUE: skipped %s — no rec_odds", m.get("title", "")[:35])

        if len(high_value) >= 2:
            valid = high_value
            logger.info(
                "Hunter VALUE filter applied: %d high-value kept, %d low-value dropped",
                len(high_value), len(low_value),
            )
        else:
            logger.info(
                "Hunter VALUE filter: only %d high-value matches — using all %d valid",
                len(high_value), valid_after_ai,
            )

        # Rank by AI confidence + league tier bonus
        # Premium leagues (CL, EL, NHL, NBA, UFC) get +0.10 effective boost,
        # minor leagues (ATP 250, ITF, challengers) get -0.10 penalty.
        # This ensures CL 72% beats ATP 250 75% in selection.
        def _sort_key(m: Dict[str, Any]) -> float:
            conf = m.get("confidence", 0)
            lg = (m.get("league") or "").lower()
            if any(kw in lg for kw in _PREMIUM_LEAGUE_KEYWORDS):
                conf += 0.10
            elif any(kw in lg for kw in _MINOR_LEAGUE_KEYWORDS):
                conf -= 0.10
            return conf

        valid.sort(key=_sort_key, reverse=True)

        # --- Diagnostic: log sorted valid candidates with effective sort key ---
        for i, m in enumerate(valid[:6], 1):
            logger.info(
                "Hunter valid-%d: conf=%.2f sort=%.2f | %s | %s | %s",
                i, m.get("confidence", 0), _sort_key(m),
                m.get("sport_slug", ""), m.get("league", "")[:30],
                m.get("title", "")[:40],
            )

        # 7. Diversify: max 2 per sport, but always try to get 3
        top3: List[Dict[str, Any]] = []
        sport_count: Dict[str, int] = defaultdict(int)
        used_ids: set = set()

        for m in valid:
            sport = m.get("sport_slug", "")
            if sport_count[sport] >= 2:
                continue
            top3.append(m)
            used_ids.add(m.get("match_id"))
            sport_count[sport] += 1
            if len(top3) == 3:
                break

        # Fallback: if < 3 after diversification, fill from remaining valid
        if len(top3) < 3:
            for m in valid:
                if m.get("match_id") in used_ids:
                    continue
                top3.append(m)
                used_ids.add(m.get("match_id"))
                if len(top3) == 3:
                    break

        # Fallback 2: if still < 3, use AI-skipped matches (prefer premium leagues)
        if len(top3) < 3 and skipped_list:
            logger.info("Hunter: only %d valid picks, filling from AI-skipped matches", len(top3))
            # Sort skipped by det_score (premium leagues first)
            skipped_list.sort(key=lambda x: x.get("det_score", 0), reverse=True)
            for m in skipped_list:
                if m.get("match_id") in used_ids:
                    continue
                # Give minimal confidence and a generic recommendation
                if not m.get("recommendation"):
                    odds_p = m.get("odds_parsed", {})
                    has_any_odds = bool(odds_p)
                    if odds_p.get("home") and odds_p.get("away"):
                        # Auto-generate: pick the favourite
                        h = _safe_float(odds_p.get("home"))
                        a = _safe_float(odds_p.get("away"))
                        if h > 0 and a > 0:
                            if h < a:
                                m["recommendation"] = "П1"
                                m["rec_odds"] = h
                            else:
                                m["recommendation"] = "П2"
                                m["rec_odds"] = a
                            m["confidence"] = 0.60
                            m["analysis_text"] = (
                                m.get("analysis_text")
                                or "Нет сильного value-сигнала — матч для наблюдения. "
                                "Рекомендация выбрана по фавориту линии."
                            )
                            m["risk"] = "средний"
                    elif odds_p.get("total_over") and odds_p.get("total_under"):
                        over = _safe_float(odds_p.get("total_over"))
                        under = _safe_float(odds_p.get("total_under"))
                        line = _safe_float(odds_p.get("total_line"))
                        if over > 1.0 and under > 1.0 and line > 0:
                            if over < under:
                                m["recommendation"] = f"ТБ {line:g}"
                                m["rec_odds"] = over
                            else:
                                m["recommendation"] = f"ТМ {line:g}"
                                m["rec_odds"] = under
                            m["confidence"] = 0.60
                            m["risk"] = "средний"
                            m["analysis_text"] = (
                                m.get("analysis_text")
                                or "Нет сильного value-сигнала — матч для наблюдения. "
                                "Рекомендация выбрана по фавориту линии тотала."
                            )
                    elif has_any_odds:
                        m["recommendation"] = "AI-анализ без ставки"
                        m["rec_odds"] = 0
                        m["confidence"] = 0.60
                        m["risk"] = "низкий/средний"
                        m["analysis_text"] = (
                            m.get("analysis_text")
                            or "Нет сильного value-сигнала — матч для наблюдения. "
                            "Коэффициенты есть, но без надёжного перекоса для ставки."
                        )
                    else:
                        # Real, verified match without reliable odds/AI signal.
                        # Keep it as an observation pick so Hunter never looks empty.
                        m["recommendation"] = "Пропуск ставки / только наблюдение"
                        m["rec_odds"] = 0
                        m["confidence"] = 0.55
                        m["risk"] = "низкий/средний"
                        m["analysis_text"] = (
                            m.get("analysis_text")
                            or "Нет сильного value-сигнала — матч для наблюдения. "
                            "Матч реальный и прошёл проверку данных, но линия/AI не дали "
                            "достаточно надёжной ставки."
                        )
                if m.get("confidence", 0) <= 0:
                    m["confidence"] = 0.60
                top3.append(m)
                used_ids.add(m.get("match_id"))
                logger.info("Hunter: filled slot with AI-skipped: %s (score=%.0f)",
                            m.get("title", "")[:40], m.get("det_score", 0))
                if len(top3) == 3:
                    break

        if not top3:
            logger.warning("Hunter: no picks at all after all fallbacks — aborting")
            _hunter_run_info["status"] = "done"
            _hunter_run_info["picks_count"] = 0
            return []

        logger.info("Hunter: FINAL %d picks selected", len(top3))

        for p in top3:
            p["pick_type"] = "top3"

        # ── Hunter v2.1 run summary ─────────────────────────────────────────
        logger.info(
            "Hunter v2.1 SUMMARY | fetched=%d → top-leagues=%d → top10=%d "
            "→ ai-valid=%d → value-filter=%d → final=%d",
            len(matches),
            len(filtered),
            candidates_count,
            valid_after_ai,
            len(valid),
            len(top3),
        )
        for i, p in enumerate(top3, 1):
            edge_pct = round(p.get("_edge", 0) * 100, 1)
            ctx_flag = "✓" if p.get("_context_text") else "✗"
            has_odds = "✓" if p.get("odds_parsed") else "✗"
            src = p.get("_data_source", "?")
            vat = (p.get("_verified_at") or "")[-8:]  # HH:MM:SS part only
            logger.info(
                "Hunter v2.1 pick #%d: %s | conf=%.0f%% | edge=%+.1f%% | ctx=%s | odds=%s | src=%s | vat=%s | %s",
                i, p.get("title", "")[:40],
                p.get("confidence", 0) * 100,
                edge_pct, ctx_flag, has_odds, src, vat,
                p.get("recommendation", ""),
            )
        # ────────────────────────────────────────────────────────────────────

        logger.info("Hunter: selected %d picks: %s",
                    len(top3),
                    [(p.get("title", "")[:30], f"{p.get('confidence', 0):.0%}") for p in top3])

        # 8. Build express
        picks = list(top3)
        if len(top3) >= 2:
            express = _build_express(top3, today)
            picks.append(express)

        # 9. Save to DB
        logger.info("Hunter STEP 6 saving=%d top3=%d", len(picks), len(top3))
        _save_picks(picks, today)
        _hunter_run_info["picks_count"] = len(top3)  # only top3, not express

        # 10. Broadcast to PRO users
        sent = 0
        if bot:
            sent = await _broadcast_to_pro_users(bot, picks, today)
        else:
            logger.info("Hunter: bot=None, skipping broadcast")

        # 11. Post to public channel
        if bot:
            await _broadcast_to_channel(bot, picks, today)

        _hunter_run_info["users_sent"] = sent
        _hunter_run_info["status"] = "done"
        logger.info("Hunter v2: done — %d picks, sent to %d users", len(top3), sent)
        return list(top3)

    except Exception as exc:
        _hunter_run_info["status"] = "error"
        _hunter_run_info["error"] = str(exc)
        logger.exception("Hunter v2: run failed")
        return []
    finally:
        # Free temp objects after heavy pipeline
        import gc
        gc.collect()
        logger.info("Hunter: gc.collect() done")


def get_hunter_status() -> Dict[str, Any]:
    """Return current Hunter run status for /hunter_status command."""
    return dict(_hunter_run_info)
