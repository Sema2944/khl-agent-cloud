# src/data_collector.py
"""
Data Collector: собирает данные из всех API-источников
и формирует структурированный MatchContext для LLM.

Pipeline: Odds API + Sports API + RSS → MatchContext → prompt_builder → LLM
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OddsData:
    """Кэфы и движение линии."""
    home_win: float = 0.0
    away_win: float = 0.0
    draw: Optional[float] = None
    total_line: float = 0.0
    total_over: float = 0.0
    total_under: float = 0.0
    # Opening odds (для движения линии)
    home_win_open: Optional[float] = None
    away_win_open: Optional[float] = None
    total_over_open: Optional[float] = None
    # Букмекер-источник
    bookmaker: str = ""


@dataclass
class TeamForm:
    """Форма команды (последние N матчей)."""
    wins: int = 0
    losses: int = 0
    otl: int = 0
    last_10: str = ""          # "6W-3L-1OTL"
    home_record: str = ""      # "8W-2L"
    away_record: str = ""      # "2W-7L-1OTL"
    streak: str = ""           # "2W" или "3L"
    goals_per_game: float = 0.0
    goals_against_per_game: float = 0.0


@dataclass
class H2HData:
    """История встреч двух команд."""
    total_games: int = 0
    home_wins: int = 0
    away_wins: int = 0
    draws: int = 0
    avg_total: float = 0.0
    last_result: str = ""      # "Сибирь 4-2"


@dataclass
class LiveStats:
    """Данные LIVE — заполняются только во время матча."""
    shots_home: int = 0
    shots_away: int = 0
    faceoffs_home: float = 0.0
    faceoffs_away: float = 0.0
    penalties_home: int = 0
    penalties_away: int = 0
    powerplay_home: str = ""   # "2/3 (67%)"
    powerplay_away: str = ""
    hits_home: int = 0
    hits_away: int = 0
    blocked_home: int = 0
    blocked_away: int = 0
    period: int = 0
    time: str = ""             # "08:34"


@dataclass
class TeamInfo:
    """Информация о команде: травмы, вратарь, усталость."""
    name: str = ""
    injuries: List[str] = field(default_factory=list)
    goalie: str = ""           # "Красотка (save% 92.1)"
    rest_days: int = 0
    travel_info: str = ""      # "перелёт из Хабаровска"


@dataclass
class MatchContext:
    """Полный контекст матча для отправки в LLM."""
    # Базовое
    match_id: str = ""
    home_team: str = ""
    away_team: str = ""
    league: str = ""
    country: str = ""
    start_time: str = ""
    status: str = ""           # "NS" / "1P" / "2P" / "3P" / "OT" / "FT"
    score_home: Optional[int] = None
    score_away: Optional[int] = None

    # Данные
    odds: Optional[OddsData] = None
    home_form: Optional[TeamForm] = None
    away_form: Optional[TeamForm] = None
    h2h: Optional[H2HData] = None
    home_info: Optional[TeamInfo] = None
    away_info: Optional[TeamInfo] = None
    live_stats: Optional[LiveStats] = None
    news: List[Dict[str, str]] = field(default_factory=list)

    def has_odds(self) -> bool:
        return self.odds is not None and self.odds.home_win > 0

    def has_form(self) -> bool:
        return self.home_form is not None and self.home_form.last_10 != ""

    def has_h2h(self) -> bool:
        return self.h2h is not None and self.h2h.total_games > 0

    def has_live_stats(self) -> bool:
        return self.live_stats is not None and self.live_stats.shots_home > 0

    def data_completeness(self) -> int:
        """% данных для confidence."""
        checks = [
            self.has_odds(),
            self.has_form(),
            self.has_h2h(),
            self.home_info is not None,
            len(self.news) > 0,
        ]
        if self.status not in ("NS", ""):
            checks.append(self.has_live_stats())
        filled = sum(checks)
        return int(filled / len(checks) * 100) if checks else 0


# ---------------------------------------------------------------------------
# In-memory cache (simple TTL dict)
# ---------------------------------------------------------------------------

from datetime import timedelta

CACHE_TTL = {
    "odds": timedelta(minutes=5),
    "form": timedelta(hours=6),
    "h2h": timedelta(hours=24),
    "news": timedelta(hours=1),
    "live_stats": timedelta(seconds=60),
    "games_today": timedelta(hours=1),
}

_cache: Dict[str, Any] = {}


def cache_get(key: str) -> Any:
    if key in _cache:
        data, expires = _cache[key]
        if datetime.utcnow() < expires:
            return data
        del _cache[key]
    return None


def cache_set(key: str, data: Any, ttl_key: str) -> None:
    if ttl_key not in CACHE_TTL:
        return
    expires = datetime.utcnow() + CACHE_TTL[ttl_key]
    _cache[key] = (data, expires)


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

async def collect_match_data(
    match_id: str,
    home_team: str = "",
    away_team: str = "",
    league: str = "",
    country: str = "",
    start_time: str = "",
    status: str = "",
    score_home: Optional[int] = None,
    score_away: Optional[int] = None,
    sport_slug: str = "ice-hockey",
    home_team_id: Optional[int] = None,
    away_team_id: Optional[int] = None,
) -> MatchContext:
    """
    Main function: collect data from all APIs and build MatchContext.
    Called before sending to LLM / PRO engine.
    Never raises — returns partial context on errors.
    """
    ctx = MatchContext(
        match_id=match_id,
        home_team=home_team,
        away_team=away_team,
        league=league,
        country=country,
        start_time=start_time,
        status=status,
        score_home=score_home,
        score_away=score_away,
    )

    # Run all API calls concurrently
    tasks = []

    # 1. Odds (The Odds API)
    tasks.append(_collect_odds(ctx, sport_slug))

    # 2. Form + H2H (api-sports.io)
    if home_team_id and away_team_id:
        tasks.append(_collect_form(ctx, home_team_id, away_team_id, sport_slug))
        tasks.append(_collect_h2h(ctx, home_team_id, away_team_id))
    elif home_team and away_team:
        tasks.append(_collect_form_by_name(ctx, sport_slug))

    # 3. News (RSS)
    tasks.append(_collect_news(ctx, sport_slug))

    # 4. Live stats (api-sports, only during match)
    if status not in ("NS", "FT", ""):
        tasks.append(_collect_live_stats(ctx, match_id, sport_slug))

    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(
        "Data collector: match=%s completeness=%d%% odds=%s form=%s h2h=%s news=%d live=%s",
        match_id, ctx.data_completeness(),
        ctx.has_odds(), ctx.has_form(), ctx.has_h2h(),
        len(ctx.news), ctx.has_live_stats(),
    )

    return ctx


# ---------------------------------------------------------------------------
# Individual collectors (each never raises)
# ---------------------------------------------------------------------------

async def _collect_odds(ctx: MatchContext, sport_slug: str) -> None:
    """Fetch odds from The Odds API."""
    cache_key = f"odds:{ctx.match_id}"
    cached = cache_get(cache_key)
    if cached:
        ctx.odds = cached
        return

    try:
        from .odds_client import get_match_odds
        odds = await get_match_odds(
            ctx.home_team, ctx.away_team, sport_slug
        )
        if odds:
            ctx.odds = odds
            cache_set(cache_key, odds, "odds")
    except Exception:
        logger.exception("Data collector: odds failed for %s", ctx.match_id)


async def _collect_form(
    ctx: MatchContext, home_id: int, away_id: int, sport_slug: str
) -> None:
    """Fetch team form from api-sports."""
    cache_key_h = f"form:{home_id}"
    cache_key_a = f"form:{away_id}"

    cached_h = cache_get(cache_key_h)
    cached_a = cache_get(cache_key_a)

    if cached_h and cached_a:
        ctx.home_form = cached_h
        ctx.away_form = cached_a
        return

    try:
        from .stats_client import get_team_form
        if not cached_h:
            hf = await get_team_form(home_id, sport_slug)
            if hf:
                ctx.home_form = hf
                cache_set(cache_key_h, hf, "form")
        else:
            ctx.home_form = cached_h

        if not cached_a:
            af = await get_team_form(away_id, sport_slug)
            if af:
                ctx.away_form = af
                cache_set(cache_key_a, af, "form")
        else:
            ctx.away_form = cached_a
    except Exception:
        logger.exception("Data collector: form failed")


async def _collect_form_by_name(ctx: MatchContext, sport_slug: str) -> None:
    """Fallback: try to find team IDs by name and fetch form."""
    try:
        from .stats_client import search_team_id, get_team_form
        home_id = await search_team_id(ctx.home_team, sport_slug)
        away_id = await search_team_id(ctx.away_team, sport_slug)
        if home_id and away_id:
            await _collect_form(ctx, home_id, away_id, sport_slug)
    except Exception:
        logger.exception("Data collector: form_by_name failed")


async def _collect_h2h(ctx: MatchContext, home_id: int, away_id: int) -> None:
    """Fetch H2H from api-sports."""
    cache_key = f"h2h:{home_id}:{away_id}"
    cached = cache_get(cache_key)
    if cached:
        ctx.h2h = cached
        return

    try:
        from .stats_client import get_h2h
        h2h = await get_h2h(home_id, away_id)
        if h2h:
            ctx.h2h = h2h
            cache_set(cache_key, h2h, "h2h")
    except Exception:
        logger.exception("Data collector: h2h failed")


async def _collect_news(ctx: MatchContext, sport_slug: str) -> None:
    """Fetch news from RSS."""
    cache_key = f"news:{ctx.home_team}:{ctx.away_team}"
    cached = cache_get(cache_key)
    if cached:
        ctx.news = cached
        return

    try:
        from .news_rss import get_team_news
        news_h = await get_team_news(ctx.home_team, sport_slug)
        news_a = await get_team_news(ctx.away_team, sport_slug)
        ctx.news = (news_h or []) + (news_a or [])
        if ctx.news:
            cache_set(cache_key, ctx.news, "news")
    except Exception:
        logger.exception("Data collector: news failed")


async def _collect_live_stats(
    ctx: MatchContext, match_id: str, sport_slug: str
) -> None:
    """Fetch live stats from api-sports."""
    cache_key = f"live:{match_id}"
    cached = cache_get(cache_key)
    if cached:
        ctx.live_stats = cached
        return

    try:
        from .stats_client import get_live_stats
        stats = await get_live_stats(match_id, sport_slug)
        if stats:
            ctx.live_stats = stats
            cache_set(cache_key, stats, "live_stats")
    except Exception:
        logger.exception("Data collector: live_stats failed")
