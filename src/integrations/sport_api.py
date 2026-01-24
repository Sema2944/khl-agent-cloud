# src/integrations/sport_api.py
from __future__ import annotations

import json# src/integrations/sport_api.py
from __future__ import annotations

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


def _get_team_name(team_obj: Any, fallback: str) -> str:
    if isinstance(team_obj, dict):
        tr = team_obj.get("translations") or team_obj.get("translation") or {}
        if isinstance(tr, dict):
            ru = tr.get("ru") or tr.get("ru_RU")
            if ru:
                return str(ru).strip()
        nm = team_obj.get("name") or team_obj.get("title")
        if nm:
            return str(nm).strip()
    if isinstance(team_obj, str) and team_obj.strip():
        return team_obj.strip()
    return fallback


def _unwrap_country(raw: Any) -> str:
    if isinstance(raw, dict):
        return _first_str(raw.get("name"), raw.get("country"), raw.get("title"))
    return _first_str(raw)


def _league_country_hint(league: str) -> str:
    league = (league or "").strip()
    if not league:
        return ""
    lower = league.lower()
    for key, country in LEAGUE_COUNTRY_HINTS.items():
        if key.lower() in lower:
            return country
    return ""


def _extract_country(raw: Dict[str, Any], tournament: Any, league: str) -> str:
    country = _unwrap_country(raw.get("country"))
    if not country:
        country = _first_str(raw.get("countryName"))

    if not country and isinstance(tournament, dict):
        country = _unwrap_country(tournament.get("country"))
        if not country:
            category = tournament.get("category")
            if isinstance(category, dict):
                country = _first_str(category.get("name"), category.get("country"))

    if not country:
        country = _league_country_hint(league)

    return country or "Other"


def _extract_score(raw: Dict[str, Any]) -> str:
    s = raw.get("score")
    if isinstance(s, str) and s.strip():
        return s.strip()

    hs = raw.get("homeScore")
    aw = raw.get("awayScore")
    if hs is not None and aw is not None:
        return f"{hs}:{aw}"

    scores = raw.get("scores")
    if isinstance(scores, dict):
        h = scores.get("home")
        a = scores.get("away")
        if h is not None and a is not None:
            return f"{h}:{a}"

    for key in ("goals", "result"):
        obj = raw.get(key)
        if isinstance(obj, dict):
            h = obj.get("home") or obj.get("homeScore") or obj.get("h")
            a = obj.get("away") or obj.get("awayScore") or obj.get("a")
            if h is not None and a is not None:
                return f"{h}:{a}"

    hs = raw.get("scoreHome") or raw.get("home_score")
    aw = raw.get("scoreAway") or raw.get("away_score")
    if hs is not None and aw is not None:
        return f"{hs}:{aw}"

    ht = raw.get("homeTeam")
    at = raw.get("awayTeam")
    if isinstance(ht, dict) and isinstance(at, dict):
        hs2 = ht.get("score")
        as2 = at.get("score")
        if hs2 is not None and as2 is not None:
            return f"{hs2}:{as2}"

    return ""


class SportAPIClient:
    """
    Sport Events API client.

    Required ENV:
      - SPORT_API_BASE (e.g. https://api.api-sport.ru)
      - SPORT_API_KEY (+ optional header/prefix)
    Optional:
      - SPORT_API_TIMEOUT_S (default 12.0)
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE", "").rstrip("/")
        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        self.headers = _auth_headers()
        self.timeout_s = _timeout()

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
            logger.info("SportAPI init: base=%r timeout=%.1f", self.base, self.timeout_s)

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        timeout = httpx.Timeout(self.timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {}, headers=self.headers)

        if r.status_code >= 400:
            txt = (r.text or "")[:500]
            raise SportAPIError(f"HTTP {r.status_code}: {txt}")

        try:
            return r.json()
        except Exception:
            raise SportAPIError(f"Bad JSON from API: {(r.text or '')[:200]}")

    def _unwrap_list(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in ("data", "response", "results", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        return []

    def _unwrap_obj(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, dict):
            for k in ("data", "response", "result", "item"):
                v = data.get(k)
                if isinstance(v, dict):
                    return v
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    def _match_to_dto(self, raw: Dict[str, Any], sport_slug: str) -> MatchDTO:
        mid = _first_str(raw.get("id"), raw.get("eventId"), raw.get("matchId"), raw.get("gameId"))

        home = _get_team_name(raw.get("homeTeam") or raw.get("home_team") or raw.get("teamHome"), "Home")
        away = _get_team_name(raw.get("awayTeam") or raw.get("away_team") or raw.get("teamAway"), "Away")

        if raw.get("home") and isinstance(raw.get("home"), str):
            home = str(raw.get("home")).strip()
        if raw.get("away") and isinstance(raw.get("away"), str):
            away = str(raw.get("away")).strip()

        league = ""
        tournament = raw.get("tournament") or raw.get("league") or raw.get("competition") or {}
        if isinstance(tournament, dict):
            tr = tournament.get("translations") or {}
            if isinstance(tr, dict) and tr.get("ru"):
                league = str(tr.get("ru")).strip()
            league = league or _first_str(tournament.get("name"), tournament.get("title"))
        else:
            league = _first_str(raw.get("leagueName"), raw.get("tournamentName"), raw.get("competitionName"))

        country = _extract_country(raw, tournament, league)

        status = _first_str(raw.get("status"), raw.get("state"), raw.get("matchStatus"), raw.get("stage"))
        start_time = _first_str(
            raw.get("dateEvent"),
            raw.get("startTime"),
            raw.get("start_date"),
            raw.get("date"),
            raw.get("time"),
        )
        score = _extract_score(raw)

        odds_base = raw.get("oddsBase") or raw.get("odds_base") or raw.get("odds")

        title = f"{home} — {away}"
        return MatchDTO(
            id=mid or "unknown",
            sport_slug=sport_slug,
            title=title,
            league=league,
            status=status,
            start_time=start_time,
            score=score,
            country=country,
            odds_base=odds_base if isinstance(odds_base, dict) else None,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        sport_slug = (sport_slug or "").strip()
        day_s = day.isoformat()

        path = f"/v2/{sport_slug}/matches"
        params = {
            "date": day_s,
            "day": day_s,
            "from": day_s,
            "to": day_s,
            "dateFrom": day_s,
            "dateTo": day_s,
            "startDate": day_s,
            "endDate": day_s,
        }

        logger.info("SportAPI try matches_by_date sport=%s: GET %s params=%s", sport_slug, path.lstrip("/"), params)

        data = await self._get_json(path, params=params)
        items = self._unwrap_list(data)
        if not items:
            raise SportAPIError(f"matches_by_date empty for {sport_slug} {day_s}")

        out = [self._match_to_dto(x, sport_slug) for x in items]
        logger.info(
            "SportAPI matches_by_date OK: requested=%s used_sport=%s used_path=%s n=%s",
            sport_slug,
            sport_slug,
            path.lstrip("/"),
            len(out),
        )

        if out:
            country_counts: Dict[str, int] = {}
            for m in out:
                c = (getattr(m, "country", "") or "Other").strip() or "Other"
                country_counts[c] = country_counts.get(c, 0) + 1

            countries_total = len(country_counts)
            other_count = country_counts.get("Other", 0)
            top = sorted(country_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
            top_str = ", ".join([f"{name}({cnt})" for name, cnt in top])
            logger.info(
                "SportAPI matches_by_date summary: matches=%s countries=%s other=%s top=%s",
                len(out),
                countries_total,
                other_count,
                top_str,
            )

        return out

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        sport_slug = (sport_slug or "").strip()
        match_id = str(match_id or "").strip()

        candidates: List[Tuple[str, Optional[Dict[str, Any]]]] = [
            (f"/v2/{sport_slug}/matches/{match_id}", None),
            (f"/v2/{sport_slug}/events/{match_id}", None),
            (f"/v2/{sport_slug}/match/{match_id}", None),
            (f"/v2/{sport_slug}/event/{match_id}", None),
        ]

        last_err: Optional[Exception] = None
        for path, params in candidates:
            try:
                data = await self._get_json(path, params=params)
                obj = self._unwrap_obj(data)
                if obj:
                    return self._match_to_dto(obj, sport_slug)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        sport_slug = (sport_slug or "").strip()
        match_id = str(match_id or "").strip()

        candidates = [
            f"/v2/{sport_slug}/matches/{match_id}/odds",
            f"/v2/{sport_slug}/events/{match_id}/odds",
            f"/v2/{sport_slug}/match/{match_id}/odds",
            f"/v2/{sport_slug}/event/{match_id}/odds",
        ]

        last_err: Optional[Exception] = None
        for path in candidates:
            try:
                data = await self._get_json(path)
                obj = self._unwrap_obj(data)
                if obj:
                    return OddsSnapshot(raw=obj)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")

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
