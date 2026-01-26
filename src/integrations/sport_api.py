# src/integrations/sport_api.py
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class SportAPIError(Exception):
    pass


LEAGUE_COUNTRY_HINTS = {
    "KHL": "Russia",
    "NHL": "USA",
    "AHL": "USA",
    "SHL": "Sweden",
    "Liiga": "Finland",
    "DEL": "Germany",
    "Extraliga": "Czech",
    "NCAA": "USA",
}

# Расширили алиасы (помогает когда провайдер не знает ice-hockey)
SPORT_ALIASES = {
    "ice-hockey": ["ice_hockey", "icehockey", "hockey", "nhl"],
    "table-tennis": ["table_tennis", "tabletennis", "ping-pong", "ping_pong"],
}


@dataclass
class MatchDTO:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str
    start_time: str
    score: str = ""
    country: str = ""
    odds_base: Optional[Dict[str, Any]] = None


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _auth_headers() -> Dict[str, str]:
    key = _env("SPORT_API_KEY")
    if not key:
        return {}
    hdr = _env("SPORT_API_KEY_HEADER", "Authorization")
    pref = _env("SPORT_API_KEY_PREFIX", "")
    val = f"{pref}{key}".strip()
    return {hdr: val} if val else {}


def _timeout() -> float:
    try:
        return float((_env("SPORT_API_TIMEOUT_S", "12.0") or "12.0"))
    except Exception:
        return 12.0


def _first_str(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _get_team_name(team_obj: Any,
