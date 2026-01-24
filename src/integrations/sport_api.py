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


class SportAPIClient:
    """
    Generic Sport Events API client.

    Required ENV:
      - SPORT_API_BASE (e.g. https://api.api-sport.ru)
      - SPORT_API_KEY (+ optional header/prefix)

    Optional ENV:
      - SPORT_API_TIMEOUT_S (default 12.0)
      - SPORT_API_SPORT_ALIASES JSON mapping
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE", "").rstrip("/")
        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")

        self.headers = _auth_headers()
        self.timeout_s = float((_env("SPORT_API_TIMEOUT_S", "12.0") or "12.0").strip() or 12.0)

        # init log (once per process start)
        try:
            from urllib.parse import urlparse

            u = urlparse(self.base)
            logger.info(
                "SportAPI init: base=%r scheme=%r host=%r header=%r prefix=%r timeout=%.1f key_present=%s",
                self.base,
                u.scheme,
                u.netloc,
                _env("SPORT_API_KEY_HEADER", "Authorization"),
                _env("SPORT_API_KEY_PREFIX", ""),
                self.timeout_s,
                bool(_env("SPORT_API_KEY")),
            )
        except Exception:
            logger.info(
                "SportAPI init: base=%r timeout=%.1f key_present=%s",
                self.base,
                self.timeout_s,
                bool(_env("SPORT_API_KEY")),
            )

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        timeout = httpx.Timeout(self.timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {}, headers=self.headers)

        if r.status_code >= 400:
            txt = (r.text or "")[:800]
            raise SportAPIError(f"HTTP {r.status_code}: {txt}")

        try:
            return r.json()
        except Exception:
            raise SportAPIError(f"Bad JSON from API: {(r.text or '')[:400]}")

    def _unwrap_list(self, data: Any) -> List[Dict[str, Any]]:
        """
        Providers often return:
        - {"data":[...]}
        - {"response":[...]}
        - {"data":{"matches":[...]}}
        - {"response":{"items":[...]}}
        - {"matches":[...]}
        - [...]
        """

        def _as_list(x: Any) -> List[Dict[str, Any]]:
            if isinstance(x, list):
                return [i for i in x if isinstance(i, dict)]
            return []

        out = _as_list(data)
        if out:
            return out

        if isinstance(data, dict):
            for k in ("data", "response", "results", "items", "matches", "events", "list"):
                out = _as_list(data.get(k))
                if out:
                    return out

            for k in ("data", "response", "result", "item"):
                v = data.get(k)
                if isinstance(v, dict):
                    for kk in ("data", "response", "results", "items", "matches", "events", "list"):
                        out = _as_list(v.get(kk))
                        if out:
                            return out

            for v in data.values():
                out = _as_list(v)
                if out:
                    return out
                if isinstance(v, dict):
                    for vv in v.values():
                        out = _as_list(vv)
                        if out:
                            return out

        return []

    def _unwrap_obj(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, dict):
            for k in ("data", "response", "result", "item", "match", "event"):
                v = data.get(k)
                if isinstance(v, dict):
                    return v
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    def _extract_score(self, raw: Dict[str, Any]) -> str:
        # 1) {"score":"2-1"} / "2:1"
        s = raw.get("score")
        if isinstance(s, str) and s.strip():
            return s.strip()

        # 2) {"homeScore":2,"awayScore":1}
        hs = raw.get("homeScore") or raw.get("home_score") or raw.get("scoreHome") or raw.get("goalsHome")
        aws = raw.get("awayScore") or raw.get("away_score") or raw.get("scoreAway") or raw.get("goalsAway")
        try:
            if hs is not None and aws is not None:
                return f"{int(hs)}:{int(aws)}"
        except Exception:
            pass

        # 3) {"scores":{"home":2,"away":1}} / {"goals":{"home":..,"away":..}}
        scores = raw.get("scores") or raw.get("goals")
        if isinstance(scores, dict):
            home = scores.get("home") or scores.get("homeScore") or scores.get("h")
            away = scores.get("away") or scores.get("awayScore") or scores.get("a")
            try:
                if home is not None and away is not None:
                    return f"{int(home)}:{int(away)}"
            except Exception:
                pass

        # 4) nested formats
        if isinstance(s, dict):
            try:
                if "home" in s and "away" in s:
                    return f"{s.get('home')}:{s.get('away')}"
                ft = s.get("fullTime")
                if isinstance(ft, dict) and "home" in ft and "away" in ft:
                    return f"{ft.get('home')}:{ft.get('away')}"
            except Exception:
                pass

        return ""

    def _match_to_dto(self, raw: Dict[str, Any], sport_slug: str) -> MatchDTO:
        # --- id ---
        mid = str(raw.get("id") or raw.get("eventId") or raw.get("matchId") or raw.get("fixture_id") or "")

        # --- teams ---
        home = (
            (raw.get("homeTeam") or {}).get("translations", {}).get("ru")
            or (raw.get("homeTeam") or {}).get("translation", {}).get("ru")
            or (raw.get("homeTeam") or {}).get("name")
            or (raw.get("home") or {}).get("name")
            or raw.get("home")
            or "Home"
        )
        away = (
            (raw.get("awayTeam") or {}).get("translations", {}).get("ru")
            or (raw.get("awayTeam") or {}).get("translation", {}).get("ru")
            or (raw.get("awayTeam") or {}).get("name")
            or (raw.get("away") or {}).get("name")
            or raw.get("away")
            or "Away"
        )

        # --- league/tournament ---
        tournament = raw.get("tournament") or raw.get("league") or {}
        league = (
            (tournament.get("translations", {}) if isinstance(tournament, dict) else {}).get("ru")
            or (tournament.get("name") if isinstance(tournament, dict) else "")
            or raw.get("leagueName")
            or ""
        )
        league = str(league or "").strip()

        # --- country (важно для навигации) ---
        country = ""
        try:
            if isinstance(raw.get("country"), str):
                country = raw.get("country") or ""

            if not country and isinstance(tournament, dict):
                c1 = tournament.get("country")
                if isinstance(c1, str) and c1.strip():
                    country = c1.strip()

                cat = tournament.get("category")
                if not country and isinstance(cat, dict):
                    cn = cat.get("name") or cat.get("country")
                    if isinstance(cn, str) and cn.strip():
                        country = cn.strip()

            if not country:
                for k in ("league_country", "leagueCountry", "countryName"):
                    v = raw.get(k)
                    if isinstance(v, str) and v.strip():
                        country = v.strip()
                        break
        except Exception:
            country = ""

        country = (country or "").strip() or "Other"

        # --- status ---
        status = str(raw.get("status") or raw.get("state") or raw.get("matchStatus") or "").strip()

        # --- start time ---
        start_time = str(
            raw.get("dateEvent")
            or raw.get("startTime")
            or raw.get("start_date")
            or raw.get("startDate")
            or raw.get("date")
            or ""
        ).strip()

        score = self._extract_score(raw)

        title = f"{home} — {away}"

        return MatchDTO(
            id=mid or "unknown",
            sport_slug=sport_slug,
            title=title,
            league=league,
            country=country,
            status=status,
            start_time=start_time,
            score=score,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        """
        Fetch matches by date with robust params/paths.
        """
        day_s = day.isoformat()
        sports = _sport_candidates(sport_slug)

        date_params = {
            "date": day_s,
            "day": day_s,
            "from": day_s,
            "to": day_s,
            "dateFrom": day_s,
            "dateTo": day_s,
            "startDate": day_s,
            "endDate": day_s,
        }

        paths: List[Tuple[str, Optional[Dict[str, Any]]]] = [
            ("/v2/{sport}/matches", date_params),
            ("/v2/{sport}/events", {"date": day_s}),
            ("/v2/{sport}/events/date/{day}", None),
            ("/v2/{sport}/games", {"date": day_s}),
        ]

        last_err: Optional[Exception] = None

        for sp in sports:
            for tpl, params in paths:
                path = tpl.format(sport=sp, day=day_s)
                try:
                    logger.info(
                        "SportAPI try matches_by_date sport=%s: GET %s params=%s",
                        sp,
                        path.lstrip("/"),
                        params or {},
                    )
                    data = await self._get_json(path, params=params)
                    items = self._unwrap_list(data)
                    if items:
                        out = [self._match_to_dto(x, sp) for x in items]
                        logger.info(
                            "SportAPI matches_by_date OK: requested=%s used_sport=%s used_path=%s n=%d",
                            sport_slug,
                            sp,
                            path.lstrip("/"),
                            len(out),
                        )
                        return out
                except Exception as e:
                    last_err = e

        if last_err:
            raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")

        raise SportAPIError(f"matches_by_date empty for {sport_slug} {day_s}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        """
        Many providers do NOT support /v2/<sport>/<id>.
        Try multiple known patterns.
        """
        sp = (sport_slug or "").strip().lower()
        mid = str(match_id or "").strip()

        if not sp or not mid:
            raise SportAPIError("match_details: missing sport_slug or match_id")

        sports = _sport_candidates(sp)

        candidates_tpl = [
            "/v2/{sport}/matches/{id}",
            "/v2/{sport}/events/{id}",
            "/v2/{sport}/match/{id}",
            "/v2/{sport}/game/{id}",
            "/v2/{sport}/{id}",  # last resort
        ]

        last_err: Optional[Exception] = None

        for s in sports:
            for tpl in candidates_tpl:
                path = tpl.format(sport=s, id=mid)
                try:
                    data = await self._get_json(path)
                    obj = self._unwrap_obj(data)
                    if obj:
                        return self._match_to_dto(obj, s)
                except Exception as e:
                    last_err = e

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        """
        Odds endpoints vary widely.
        """
        sp = (sport_slug or "").strip().lower()
        mid = str(match_id or "").strip()
        if not sp or not mid:
            raise SportAPIError("match_odds: missing sport_slug or match_id")

        sports = _sport_candidates(sp)

        candidates_tpl = [
            "/v2/{sport}/events/{id}/odds",
            "/v2/{sport}/matches/{id}/odds",
            "/v2/{sport}/odds/{id}",
            "/v2/{sport}/match/{id}/odds",
        ]

        last_err: Optional[Exception] = None

        for s in sports:
            for tpl in candidates_tpl:
                path = tpl.format(sport=s, id=mid)
                try:
                    data = await self._get_json(path)
                    obj = self._unwrap_obj(data)
                    if obj:
                        return OddsSnapshot(raw=obj)
                except Exception as e:
                    last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")
