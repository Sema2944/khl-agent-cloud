# src/sports_config.py
"""
Centralized sports configuration for all api-sports.io endpoints.
One API key ($19/mo) covers all sports.

To enable a new sport: set "enabled": True → it auto-appears in the bot menu.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# ============================================================
# FULL SPORTS CONFIG
# ============================================================

SPORTS_CONFIG: Dict[str, Dict[str, Any]] = {

    # ==========================================
    # ⚽ ФУТБОЛ — Приоритет 1
    # ==========================================
    "football": {
        "emoji": "⚽",
        "name": "Футбол",
        "slug": "football",             # internal slug
        "bot_slug": "football",          # slug used in bot callbacks
        "enabled": True,
        "api_base": "https://v3.football.api-sports.io",
        "endpoints": {
            "fixtures":   "/fixtures",
            "statistics": "/fixtures/statistics",
            "h2h":        "/fixtures/headtohead",
            "lineups":    "/fixtures/lineups",
            "standings":  "/standings",
            "injuries":   "/injuries",
        },
        "match_param": "id",              # ?id=123 (api-sports.io v3 football)
        "leagues": {
            # api-sports.io football: season = год начала (2025-26 сезон = season 2025)
            # Россия
            235: {"name": "РПЛ", "flag": "🇷🇺", "country": "Россия", "season": 2025, "priority": 1},
            237: {"name": "Кубок России", "flag": "🇷🇺", "country": "Россия", "season": 2025, "priority": 2},
            # Англия
            39:  {"name": "Premier League", "flag": "🏴", "country": "Англия", "season": 2025, "priority": 1},
            45:  {"name": "FA Cup", "flag": "🏴", "country": "Англия", "season": 2025, "priority": 2},
            # Испания
            140: {"name": "La Liga", "flag": "🇪🇸", "country": "Испания", "season": 2025, "priority": 1},
            143: {"name": "Copa del Rey", "flag": "🇪🇸", "country": "Испания", "season": 2025, "priority": 2},
            # Германия
            78:  {"name": "Bundesliga", "flag": "🇩🇪", "country": "Германия", "season": 2025, "priority": 1},
            # Италия
            135: {"name": "Serie A", "flag": "🇮🇹", "country": "Италия", "season": 2025, "priority": 1},
            # Франция
            61:  {"name": "Ligue 1", "flag": "🇫🇷", "country": "Франция", "season": 2025, "priority": 1},
            # Еврокубки
            2:   {"name": "Лига Чемпионов", "flag": "🇪🇺", "country": "Европа", "season": 2025, "priority": 1},
            3:   {"name": "Лига Европы", "flag": "🇪🇺", "country": "Европа", "season": 2025, "priority": 1},
            848: {"name": "Лига Конференций", "flag": "🇪🇺", "country": "Европа", "season": 2025, "priority": 2},
            # Турция
            203: {"name": "Super Lig", "flag": "🇹🇷", "country": "Турция", "season": 2025, "priority": 2},
            # Португалия
            94:  {"name": "Primeira Liga", "flag": "🇵🇹", "country": "Португалия", "season": 2025, "priority": 2},
            # Нидерланды
            88:  {"name": "Eredivisie", "flag": "🇳🇱", "country": "Нидерланды", "season": 2025, "priority": 2},
        },
        # Odds API sport keys
        "odds_keys": [
            "soccer_russia_premier_league",
            "soccer_epl",
            "soccer_spain_la_liga",
            "soccer_germany_bundesliga",
            "soccer_italy_serie_a",
            "soccer_france_ligue_one",
            "soccer_uefa_champs_league",
            "soccer_uefa_europa_league",
        ],
        # RSS feeds
        "rss_feeds": [
            "https://www.championat.com/football/rss.xml",
            "https://sport-express.ru/football/rss/",
        ],
    },

    # ==========================================
    # 🏒 ХОККЕЙ — Приоритет 1
    # ==========================================
    "ice-hockey": {
        "emoji": "🏒",
        "name": "Хоккей",
        "slug": "ice-hockey",
        "bot_slug": "ice-hockey",
        "enabled": True,
        "api_base": "https://v1.hockey.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/games/statistics",
            "h2h":        "/games/h2h",
            "lineups":    "/games/lineups",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            50:  {"name": "KHL", "flag": "🇷🇺", "country": "Россия", "season": 2025, "priority": 1},
            57:  {"name": "NHL", "flag": "🇺🇸", "country": "USA/Canada", "season": 2025, "priority": 1},
            48:  {"name": "VHL", "flag": "🇷🇺", "country": "Россия", "season": 2025, "priority": 2},
            56:  {"name": "SHL", "flag": "🇸🇪", "country": "Швеция", "season": 2025, "priority": 2},
            51:  {"name": "Liiga", "flag": "🇫🇮", "country": "Финляндия", "season": 2025, "priority": 2},
            52:  {"name": "Extraliga", "flag": "🇨🇿", "country": "Чехия", "season": 2025, "priority": 3},
            53:  {"name": "DEL", "flag": "🇩🇪", "country": "Германия", "season": 2025, "priority": 3},
        },
        "odds_keys": [
            "icehockey_nhl",
            "icehockey_sweden_hockey_league",
            "icehockey_liiga",
        ],
        "rss_feeds": [
            "https://www.championat.com/hockey/_khl/rss.xml",
            "https://sport-express.ru/hockey/khl/rss/",
        ],
    },

    # ==========================================
    # 🎾 ТЕННИС — Приоритет 1
    # ==========================================
    "tennis": {
        "emoji": "🎾",
        "name": "Теннис",
        "slug": "tennis",
        "bot_slug": "tennis",
        "enabled": True,  # ATP+WTA via ESPN free API (no key needed)
        "api_base": "",  # ESPN public API, no base needed
        "endpoints": {},
        "match_param": "id",
        "leagues": {},  # Tournaments loaded dynamically from ESPN
        "odds_keys": [
            "tennis_atp_australian_open",
            "tennis_atp_french_open",
            "tennis_atp_us_open",
            "tennis_wta_australian_open",
            "tennis_wta_french_open",
            "tennis_wta_us_open",
        ],
        "rss_feeds": [],
    },

    # ==========================================
    # 🏀 БАСКЕТБОЛ — Приоритет 1
    # ==========================================
    "basketball": {
        "emoji": "🏀",
        "name": "Баскетбол",
        "slug": "basketball",
        "bot_slug": "basketball",
        "enabled": True,  # NBA via ESPN free API (no key needed)
        "api_base": "https://v1.basketball.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/statistics",
            "h2h":        "/games",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            12:  {"name": "NBA", "flag": "🇺🇸", "country": "USA", "season": "2025-2026", "priority": 1},
            120: {"name": "Euroleague", "flag": "🇪🇺", "country": "Европа", "season": "2025-2026", "priority": 1},
            117: {"name": "Eurocup", "flag": "🇪🇺", "country": "Европа", "season": "2025-2026", "priority": 2},
            179: {"name": "VTB League", "flag": "🇷🇺", "country": "Россия", "season": "2025-2026", "priority": 2},
        },
        "odds_keys": [
            "basketball_nba",
            "basketball_euroleague",
        ],
        "rss_feeds": [
            "https://www.championat.com/basketball/rss.xml",
        ],
    },

    # ==========================================
    # 🏐 ВОЛЕЙБОЛ — Приоритет 2
    # ==========================================
    "volleyball": {
        "emoji": "🏐",
        "name": "Волейбол",
        "slug": "volleyball",
        "bot_slug": "volleyball",
        "enabled": False,  # disabled: api-sports.io free plan blocks current season
        "api_base": "https://v1.volleyball.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/games/statistics",
            "h2h":        "/games/h2h",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            78:  {"name": "Суперлига", "flag": "🇷🇺", "country": "Россия", "season": 2025, "priority": 1},
            37:  {"name": "CEV Champions League", "flag": "🇪🇺", "country": "Европа", "season": 2025, "priority": 2},
            30:  {"name": "Serie A1", "flag": "🇮🇹", "country": "Италия", "season": 2025, "priority": 2},
        },
        "odds_keys": [],
        "rss_feeds": [],
    },

    # ==========================================
    # 🤾 ГАНДБОЛ — Приоритет 2
    # ==========================================
    "handball": {
        "emoji": "🤾",
        "name": "Гандбол",
        "slug": "handball",
        "bot_slug": "handball",
        "enabled": False,  # disabled: api-sports.io free plan blocks current season
        "api_base": "https://v1.handball.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/games/statistics",
            "h2h":        "/games/h2h",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            35:  {"name": "EHF Champions League", "flag": "🇪🇺", "country": "Европа", "season": 2025, "priority": 1},
            34:  {"name": "Bundesliga", "flag": "🇩🇪", "country": "Германия", "season": 2025, "priority": 2},
        },
        "odds_keys": [],
        "rss_feeds": [],
    },

    # ==========================================
    # 🥊 MMA — Приоритет 2
    # ==========================================
    "mma": {
        "emoji": "🥊",
        "name": "MMA",
        "slug": "mma",
        "bot_slug": "mma",
        "enabled": True,  # UFC via ESPN free API (no key needed)
        "api_base": "https://v1.mma.api-sports.io",
        "endpoints": {
            "fixtures": "/fights",
            "h2h":      "/fights/h2h",
            "standings": "/rankings",
        },
        "match_param": "id",
        "leagues": {
            1:  {"name": "UFC", "flag": "🇺🇸", "country": "USA", "season": 2025, "priority": 1},
        },
        "odds_keys": [
            "mma_mixed_martial_arts",
        ],
        "rss_feeds": [],
    },

    # ==========================================
    # 🏎 ФОРМУЛА-1 — Приоритет 3 (выключен)
    # ==========================================
    "formula1": {
        "emoji": "🏎",
        "name": "Формула-1",
        "slug": "formula1",
        "bot_slug": "formula1",
        "enabled": False,
        "api_base": "https://v1.formula-1.api-sports.io",
        "endpoints": {
            "fixtures": "/races",
            "standings": "/rankings/drivers",
        },
        "match_param": "id",
        "leagues": {
            1: {"name": "Formula 1", "flag": "🏁", "country": "Мир", "season": 2026, "priority": 1},
        },
        "odds_keys": [],
        "rss_feeds": [],
    },

    # ==========================================
    # ⚾ БЕЙСБОЛ — Приоритет 3 (выключен)
    # ==========================================
    "baseball": {
        "emoji": "⚾",
        "name": "Бейсбол",
        "slug": "baseball",
        "bot_slug": "baseball",
        "enabled": False,
        "api_base": "https://v1.baseball.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/games/statistics",
            "h2h":        "/games/h2h",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            1:  {"name": "MLB", "flag": "🇺🇸", "country": "USA", "season": 2026, "priority": 1},
        },
        "odds_keys": [],
        "rss_feeds": [],
    },

    # ==========================================
    # 🏉 РЕГБИ — Приоритет 3 (выключен)
    # ==========================================
    "rugby": {
        "emoji": "🏉",
        "name": "Регби",
        "slug": "rugby",
        "bot_slug": "rugby",
        "enabled": False,
        "api_base": "https://v1.rugby.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "h2h":        "/games/h2h",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            44: {"name": "Six Nations", "flag": "🇪🇺", "country": "Европа", "season": 2026, "priority": 1},
        },
        "odds_keys": [],
        "rss_feeds": [],
    },

    # ==========================================
    # 🏈 NFL — Приоритет 3 (выключен)
    # ==========================================
    "american-football": {
        "emoji": "🏈",
        "name": "Американский футбол",
        "slug": "american-football",
        "bot_slug": "american-football",
        "enabled": False,
        "api_base": "https://v1.american-football.api-sports.io",
        "endpoints": {
            "fixtures":   "/games",
            "statistics": "/games/statistics",
            "h2h":        "/games/h2h",
            "standings":  "/standings",
        },
        "match_param": "id",
        "leagues": {
            1: {"name": "NFL", "flag": "🇺🇸", "country": "USA", "season": 2025, "priority": 1},
            2: {"name": "NCAA", "flag": "🇺🇸", "country": "USA", "season": 2025, "priority": 2},
        },
        "odds_keys": [
            "americanfootball_nfl",
        ],
        "rss_feeds": [],
    },
}


# ============================================================
# HELPERS
# ============================================================

def get_enabled_sports() -> Dict[str, Dict[str, Any]]:
    """Return only enabled sports."""
    return {k: v for k, v in SPORTS_CONFIG.items() if v.get("enabled")}


def get_sport_labels() -> Dict[str, str]:
    """Return {slug: 'emoji name'} for enabled sports."""
    return {k: f"{v['emoji']} {v['name']}" for k, v in get_enabled_sports().items()}


def get_default_sports() -> List[str]:
    """Return list of enabled sport slugs (ordered)."""
    return list(get_enabled_sports().keys())


def get_sport_config(slug: str) -> Optional[Dict[str, Any]]:
    """Get config for a sport by slug. Also checks aliases."""
    if slug in SPORTS_CONFIG:
        return SPORTS_CONFIG[slug]
    # Alias: "hockey" → "ice-hockey"
    for key, cfg in SPORTS_CONFIG.items():
        if slug == cfg.get("slug") or slug in _SPORT_ALIASES.get(key, []):
            return cfg
    return None


def get_api_base(slug: str) -> Optional[str]:
    """Get API base URL for a sport."""
    cfg = get_sport_config(slug)
    return cfg["api_base"] if cfg else None


def get_leagues(slug: str, max_priority: int = 99) -> Dict[int, Dict[str, Any]]:
    """Get leagues for a sport, optionally filtered by priority."""
    cfg = get_sport_config(slug)
    if not cfg:
        return {}
    return {
        lid: info for lid, info in cfg.get("leagues", {}).items()
        if info.get("priority", 99) <= max_priority
    }


def get_odds_keys(slug: str) -> List[str]:
    """Get Odds API sport keys for a sport."""
    cfg = get_sport_config(slug)
    return cfg.get("odds_keys", []) if cfg else []


def get_rss_feeds(slug: str) -> List[str]:
    """Get RSS feed URLs for a sport."""
    cfg = get_sport_config(slug)
    return cfg.get("rss_feeds", []) if cfg else []


def get_match_param(slug: str) -> str:
    """Get the match ID parameter name for API requests."""
    cfg = get_sport_config(slug)
    return cfg.get("match_param", "id") if cfg else "id"


def get_endpoints(slug: str) -> Dict[str, str]:
    """Get API endpoints for a sport."""
    cfg = get_sport_config(slug)
    return cfg.get("endpoints", {}) if cfg else {}


# Sport slug aliases (for backward compatibility)
_SPORT_ALIASES: Dict[str, List[str]] = {
    "ice-hockey": ["hockey", "ice_hockey", "icehockey"],
    "american-football": ["american_football", "nfl"],
    "formula1": ["f1", "formula-1"],
    "tennis": ["atp", "wta"],
    "mma": ["ufc", "mixed-martial-arts"],
}


def resolve_sport_slug(raw: str) -> Optional[str]:
    """Resolve any sport name/alias to canonical slug."""
    raw = (raw or "").strip().lower()
    if raw in SPORTS_CONFIG:
        return raw
    for key, aliases in _SPORT_ALIASES.items():
        if raw in aliases:
            return key
    return None
