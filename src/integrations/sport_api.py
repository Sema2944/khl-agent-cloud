# src/integrations/sport_api.py
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ============================================================
# Errors
# ============================================================
class SportAPIError(RuntimeError):
    pass


# ============================================================
# Helpers
# ============================================================
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    path = (path or "").lstrip("/")
    return f"{base}/{path}"


# ============================================================
# Data models
# ============================================================
@dataclass
class MatchItem:
    id: str
    sport_slug: str
    title: str
    country: str
    league: str
    status: str
    start_time: str
    score: str = ""
    odds_base: Optional[Dict[str, Any]] = None


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


# ============================================================
# Client
# ============================================================
class SportAPIClient:
    """
    Клиент под api.api-sport.ru

    ENV:
      SPORT_API_BASE=https://api.api-sport.ru
      SPORT_API_KEY=<key>
      SPORT_API_KEY_HEADER=Authorization
      SPORT_API_KEY_PREFIX=
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE")
        self.key = _env("SPORT_API_KEY")
        self.key_header = _env("SPORT_API_KEY_HEADER", "Authorization")
        self.key_prefix = _env("SPORT_API_KEY_PREFIX", "")
        self.timeout_s = float(_env("SPORT_API_TIMEOUT_S", "12") or 12)
        self.base_path = _env("SPORT_API_BASE_PATH", "").strip("/")

        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        if not self.key:
            raise SportAPIError("SPORT_API_KEY is missing")

        u = urlparse(self.base)
        logger.info(
            "SportAPI init: base=%s scheme=%s host=%s header=%s prefix=%s timeout=%s",
            self.base,
            u.scheme,
            u.netloc,
            self.key_header,
            self.key_prefix,
            self.timeout_s,
        )

    # ---------------- HTTP ----------------
    def _headers(self) -> Dict[str, str]:
        val = self.key
        if self.key_prefix:
            val = f"{self.key_prefix} {val}".strip()
        return {"Accept": "application/json", self.key_header: val}

    def _normalize_path(self, path: str) -> str:
        path = (path or "").lstrip("/")
        if self.base_path:
            return f"{self.base_path}/{path}".lstrip("/")
        return path

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = _join_url(self.base, self._normalize_path(path))
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                r = await client.get(url, headers=self._headers(), params=params)
            except Exception as e:
                raise SportAPIError(f"request failed: {e}") from e

        if r.status_code >= 400:
            raise SportAPIError(f"HTTP {r.status_code}: {r.text[:500]}")

        try:
            return r.json()
        except Exception as e:
            raise SportAPIError(f"bad json: {e}") from e

    # ---------------- parsing helpers ----------------
    def _pick(self, d: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default

    def _team_name(self, v: Any) -> str:
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            tr = v.get("translations")
            if isinstance(tr, dict):
                ru = tr.get("ru")
                if isinstance(ru, str) and ru.strip():
                    return ru.strip()
            name = v.get("name")
            if isinstance(name, str):
                return name.strip()
        return ""

    def _infer_title(self, m: Dict[str, Any]) -> str:
        t = self._pick(m, ["title", "name"], "")
        if isinstance(t, str) and t.strip():
            return t.strip()

        h = self._team_name(m.get("home"))
        a = self._team_name(m.get("away"))
        if h and a:
            return f"{h} — {a}"

        teams = m.get("teams")
        if isinstance(teams, list) and len(teams) >= 2:
            return f"{self._team_name(teams[0])} — {self._team_name(teams[1])}"

        return "Матч"

    def _infer_league(self, m: Dict[str, Any]) -> str:
        lg = self._pick(m, ["league", "competition", "tournament"], "")
        if isinstance(lg, dict):
            return str(self._pick(lg, ["name", "title"], "")).strip()
        return str(lg or "").strip()

    def _infer_country(self, m: Dict[str, Any]) -> str:
        lg = m.get("league") or m.get("competition")
        if isinstance(lg, dict):
            c = lg.get("country")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, dict):
                return str(c.get("name") or "").strip()

        for side in ("home", "away"):
            t = m.get(side)
            if isinstance(t, dict):
                c = t.get("country")
                if isinstance(c, str):
                    return c.strip()

        return "Other"

    def _infer_status(self, m: Dict[str, Any]) -> str:
        s = self._pick(m, ["status", "state"], "")
        if isinstance(s, dict):
            return str(self._pick(s, ["name", "type"], "")).strip()
        return str(s or "").strip()

    def _infer_start_time(self, m: Dict[str, Any]) -> str:
        st = self._pick(m, ["startTime", "start_time", "date"], "")
        if isinstance(st, dict):
            return str(self._pick(st, ["iso", "utc"], "")).strip()
        return str(st or "").strip()

    def _infer_score(self, m: Dict[str, Any]) -> str:
        s = m.get("score")
        if isinstance(s, dict):
            h = s.get("home", {}).get("current")
            a = s.get("away", {}).get("current")
            if h is not None and a is not None:
                return f"{h}:{a}"
        return ""

    def _infer_odds_base(self, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for k in ("oddsBase", "odds", "markets", "line"):
            v = m.get(k)
            if isinstance(v, dict):
                return v
            if isinstance(v, list):
                return {"items": v}
        return None

    # ============================================================
    # Public API
    # ============================================================
    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchItem]:
        day_s = day.isoformat()

        params = {
            "date": day_s,
            "from": day_s,
            "to": day_s,
        }

        candidates = [
            f"v2/{sport_slug}/matches",
            f"v2/{sport_slug}/events",
            f"v2/{sport_slug}",
        ]

        data = None
        last_err = None

        for p in candidates:
            try:
                data = await self._get(p, params=params)
                break
            except Exception as e:
                last_err = e

        if data is None:
            raise SportAPIError(f"matches_by_date failed: {last_err}")

        items = None
        if isinstance(data, dict):
            for k in ("data", "events", "matches"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list):
            raise SportAPIError("unexpected response shape")

        out: List[MatchItem] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = str(self._pick(m, ["id", "eventId"], "")).strip()
            if not mid:
                continue

            out.append(
                MatchItem(
                    id=mid,
                    sport_slug=sport_slug,
                    title=self._infer_title(m),
                    country=self._infer_country(m),
                    league=self._infer_league(m),
                    status=self._infer_status(m),
                    start_time=self._infer_start_time(m),
                    score=self._infer_score(m),
                    odds_base=self._infer_odds_base(m),
                )
            )

        return out

    async def match_details(self, sport_slug: str, match_id: str) -> MatchItem:
        data = await self._get(f"v2/{sport_slug}/{match_id}")

        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            m = data["data"]
        elif isinstance(data, dict):
            m = data
        else:
            raise SportAPIError("unexpected response shape")

        return MatchItem(
            id=str(match_id),
            sport_slug=sport_slug,
            title=self._infer_title(m),
            country=self._infer_country(m),
            league=self._infer_league(m),
            status=self._infer_status(m),
            start_time=self._infer_start_time(m),
            score=self._infer_score(m),
            odds_base=self._infer_odds_base(m),
        )


# ============================================================
# Compatibility exports (ВАЖНО)
# ============================================================
SportAPI = SportAPIClient

__all__ = [
    "SportAPIError",
    "MatchItem",
    "OddsSnapshot",
    "SportAPIClient",
    "SportAPI",
]
