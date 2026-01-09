# src/integrations/sport_api.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import httpx


class SportAPIError(RuntimeError):
    pass


@dataclass
class ApiMatch:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str
    start_time: str
    score: str = ""
    odds_base: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None


class SportAPIClient:
    """
    Основано на твоей OpenAPI (sport-events-api.json):
    - GET /v2/{sportSlug}/matches?date=YYYY-MM-DD
    - GET /v2/{sportSlug}/matches/{matchId}
    """

    def __init__(self) -> None:
        self.base = (os.getenv("SPORT_API_BASE") or "").strip().rstrip("/")
        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")

        self.api_key = (os.getenv("SPORT_API_KEY") or "").strip()
        self.key_header = (os.getenv("SPORT_API_KEY_HEADER") or "X-API-KEY").strip()
        self.key_prefix = (os.getenv("SPORT_API_KEY_PREFIX") or "").strip()  # например: "Bearer"

        timeout_s = float(os.getenv("SPORT_API_TIMEOUT_S") or "12")
        self._timeout = httpx.Timeout(timeout_s)

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            v = f"{self.key_prefix} {self.api_key}".strip() if self.key_prefix else self.api_key
            headers[self.key_header] = v
        return headers

    @staticmethod
    def _safe_get(d: Dict[str, Any], path: List[str], default: str = "") -> str:
        cur: Any = d
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return "" if cur is None else str(cur)

    @staticmethod
    def _fmt_score(raw: Dict[str, Any]) -> str:
        # в схеме есть score, но поля могут различаться по видам спорта
        score = raw.get("score")
        if not isinstance(score, dict):
            return ""
        # самые частые варианты
        home = score.get("home") or score.get("homeScore") or score.get("homeTotal")
        away = score.get("away") or score.get("awayScore") or score.get("awayTotal")
        if home is None or away is None:
            return ""
        return f"{home}:{away}"

    @staticmethod
    def _title_from_match(raw: Dict[str, Any]) -> str:
        ht = raw.get("homeTeam") or {}
        at = raw.get("awayTeam") or {}
        h = ht.get("name") or ht.get("translation") or "Home"
        a = at.get("name") or at.get("translation") or "Away"
        return f"{h} — {a}"

    @staticmethod
    def _league_from_match(raw: Dict[str, Any]) -> str:
        t = raw.get("tournament")
        if isinstance(t, dict):
            # tournament.name чаще всего и есть “лига”
            name = t.get("name") or ""
            # иногда полезно дополнить категорией
            cat = t.get("category")
            if isinstance(cat, dict):
                c = cat.get("name") or ""
                if c and c not in name:
                    return f"{name} · {c}".strip(" ·")
            return str(name)
        return ""

    async def matches_by_date(self, sport_slug: str, d: date) -> List[ApiMatch]:
        url = f"{self.base}/v2/{sport_slug}/matches"
        params = {"date": d.isoformat()}

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
                r = await client.get(url, params=params)
        except Exception as e:
            raise SportAPIError(f"API request failed: {e}")

        if r.status_code >= 400:
            raise SportAPIError(f"HTTP {r.status_code}: {r.text[:200]}")

        payload = r.json()
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(matches, list):
            return []

        out: List[ApiMatch] = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "")
            if not mid:
                continue
            out.append(
                ApiMatch(
                    id=mid,
                    sport_slug=sport_slug,
                    title=self._title_from_match(m),
                    league=self._league_from_match(m),
                    status=str(m.get("status") or ""),
                    start_time=str(m.get("startTime") or ""),
                    score=self._fmt_score(m),
                    odds_base=m.get("oddsBase") if isinstance(m.get("oddsBase"), dict) else None,
                    raw=m,
                )
            )
        return out

    async def match_details(self, sport_slug: str, match_id: str) -> ApiMatch:
        url = f"{self.base}/v2/{sport_slug}/matches/{match_id}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
                r = await client.get(url)
        except Exception as e:
            raise SportAPIError(f"API request failed: {e}")

        if r.status_code >= 400:
            raise SportAPIError(f"HTTP {r.status_code}: {r.text[:200]}")

        m = r.json()
        if not isinstance(m, dict):
            raise SportAPIError("Unexpected match_details payload")

        return ApiMatch(
            id=str(m.get("id") or match_id),
            sport_slug=sport_slug,
            title=self._title_from_match(m),
            league=self._league_from_match(m),
            status=str(m.get("status") or ""),
            start_time=str(m.get("startTime") or ""),
            score=self._fmt_score(m),
            odds_base=m.get("oddsBase") if isinstance(m.get("oddsBase"), dict) else None,
            raw=m,
        )
