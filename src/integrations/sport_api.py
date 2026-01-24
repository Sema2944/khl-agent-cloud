# src/integrations/sport_api.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class SportAPIError(Exception):
    pass


@dataclass
class MatchDTO:
    id: str
    sport_slug: str
    title: str
    league: str
    country: str
    status: str
    start_time: str
    score: str = ""


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
    return {hdr: f"{pref}{key}".strip()}


def _parse_sport_aliases() -> Dict[str, List[str]]:
    """
    Optional ENV:
      SPORT_API_SPORT_ALIASES='{"ice-hockey":["ice-hockey","hockey"]}'
    """
    raw = _env("SPORT_API_SPORT_ALIASES", "")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            out: Dict[str, List[str]] = {}
            for k, v in obj.items():
                if not isinstance(k, str):
                    continue
                kk = k.strip().lower()
                if isinstance(v, list):
                    out[kk] = [str(x).strip().lower() for x in v if str(x).strip()]
                elif isinstance(v, str) and v.strip():
                    out[kk] = [v.strip().lower()]
            return out
    except Exception:
        logger.exception("SPORT_API_SPORT_ALIASES invalid JSON")
    return {}


_SPORT_ALIASES = _parse_sport_aliases()


def _sport_candidates(sport_slug: str) -> List[str]:
    s = (sport_slug or "").strip().lower()
    if not s:
        return []

    # aliases from ENV
    if s in _SPORT_ALIASES and _SPORT_ALIASES[s]:
        xs = [s] + _SPORT_ALIASES[s]
        seen = set()
        out: List[str] = []
        for x in xs:
            x = (x or "").strip().lower()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    # common heuristics
    if s == "ice-hockey":
        return ["ice-hockey", "hockey"]

    if s == "table-tennis":
        return ["table-tennis", "ping-pong"]

    return [s]
