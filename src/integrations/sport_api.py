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

# Алиасы спорта (когда провайдер не знает исходный slug)
SPORT_ALIASES: Dict[str, List[str]] = {
    "ice-hockey": ["hockey"],
    "table-tennis": ["ping-pong", "table_tennis", "tabletennis"],
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
        return float((_env("SPORT_API_TIMEOUT_S", "12.0") or "12.0").strip())
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
        country = _first_str(raw.get("countryName"), raw.get("leagueCountry"), raw.get("country_name"))

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

    # homeScore/awayScore
    hs = raw.get("homeScore") or raw.get("home_score") or raw.get("scoreHome") or raw.get("goalsHome")
    aw = raw.get("awayScore") or raw.get("away_score") or raw.get("scoreAway") or raw.get("goalsAway")
    if hs is not None and aw is not None:
        return f"{hs}:{aw}"

    # scores/goals/result objects
    for key in ("scores", "goals", "result"):
        obj = raw.get(key)
        if isinstance(obj, dict):
            h = obj.get("home") or obj.get("homeScore") or obj.get("h")
            a = obj.get("away") or obj.get("awayScore") or obj.get("a")
            if h is not None and a is not None:
                return f"{h}:{a}"

    # sometimes score is dict
    if isinstance(s, dict):
        if "home" in s and "away" in s:
            return f"{s.get('home')}:{s.get('away')}"
        ft = s.get("fullTime")
        if isinstance(ft, dict) and "home" in ft and "away" in ft:
            return f"{ft.get('home')}:{ft.get('away')}"

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

    def _match_to_dto(self, raw: Dict[str, Any], sport_slug: str) -> MatchDTO:
        mid = _first_str(raw.get("id"), raw.get("eventId"), raw.get("matchId"), raw.get("gameId"), raw.get("fixture_id"))

        home = _get_team_name(
            raw.get("homeTeam") or raw.get("home_team") or raw.get("teamHome") or raw.get("home"),
            "Home",
        )
        away = _get_team_name(
            raw.get("awayTeam") or raw.get("away_team") or raw.get("teamAway") or raw.get("away"),
            "Away",
        )

        # если home/away пришли строкой
        if isinstance(raw.get("home"), str) and raw.get("home").strip():
            home = str(raw.get("home")).strip()
        if isinstance(raw.get("away"), str) and raw.get("away").strip():
            away = str(raw.get("away")).strip()

        league = ""
        tournament = raw.get("tournament") or raw.get("league") or raw.get("competition") or {}
        if isinstance(tournament, dict):
            tr = tournament.get("translations") or tournament.get("translation") or {}
            if isinstance(tr, dict):
                league = _first_str(tr.get("ru"), tr.get("ru_RU"))
            league = league or _first_str(tournament.get("name"), tournament.get("title"))
        else:
            league = _first_str(raw.get("leagueName"), raw.get("tournamentName"), raw.get("competitionName"))

        country = _extract_country(raw, tournament, league)

        status = _first_str(raw.get("status"), raw.get("state"), raw.get("matchStatus"), raw.get("stage"))
        start_time = _first_str(
            raw.get("dateEvent"),
            raw.get("startTime"),
            raw.get("start_date"),
            raw.get("startDate"),
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
            league=str(league or "").strip(),
            status=str(status or "").strip(),
            start_time=str(start_time or "").strip(),
            score=str(score or "").strip(),
            country=str(country or "").strip() or "Other",
            odds_base=odds_base if isinstance(odds_base, dict) else None,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        """
        Пробуем разные пути и алиасы спорта.
        Главное: не падаем на 404 одного эндпоинта — пробуем дальше.
        """
        sport_slug = (sport_slug or "").strip().lower()
        if not sport_slug:
            raise SportAPIError("matches_by_date: sport_slug is empty")

        day_s = day.isoformat()

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

        candidates = [sport_slug]
        for x in SPORT_ALIASES.get(sport_slug, []):
            xx = (x or "").strip().lower()
            if xx and xx not in candidates:
                candidates.append(xx)

        paths: List[Tuple[str, Optional[Dict[str, Any]]]] = [
            ("/v2/{sport}/matches", params),
            ("/v2/{sport}/events", {"date": day_s}),
            ("/v2/{sport}/games", {"date": day_s}),
            ("/v2/{sport}/events/date/{day}", None),
        ]

        last_err: Optional[Exception] = None

        for cand in candidates:
            for tpl, p in paths:
                path = tpl.format(sport=cand, day=day_s)
                try:
                    logger.info(
                        "SportAPI try matches_by_date sport=%s: GET %s params=%s",
                        cand,
                        path.lstrip("/"),
                        p or {},
                    )
                    data = await self._get_json(path, params=p)
                    items = self._unwrap_list(data)
                    if items:
                        out = [self._match_to_dto(x, sport_slug) for x in items]
                        logger.info(
                            "SportAPI matches_by_date OK: requested=%s used_sport=%s used_path=%s n=%d",
                            sport_slug,
                            cand,
                            path.lstrip("/"),
                            len(out),
                        )

                        # небольшой лог-саммари (помогает дебажить "Other")
                        country_counts: Dict[str, int] = {}
                        for m in out:
                            c = (m.country or "Other").strip() or "Other"
                            country_counts[c] = country_counts.get(c, 0) + 1
                        top = sorted(country_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
                        top_str = ", ".join([f"{name}({cnt})" for name, cnt in top])
                        logger.info(
                            "SportAPI matches_by_date summary: matches=%s countries=%s other=%s top=%s",
                            len(out),
                            len(country_counts),
                            country_counts.get("Other", 0),
                            top_str,
                        )

                        return out
                except Exception as e:
                    last_err = e
                    # продолжаем перебирать

        if last_err:
            raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")

        raise SportAPIError(f"matches_by_date empty for {sport_slug} {day_s}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id or "").strip()
        if not sport_slug or not match_id:
            raise SportAPIError("match_details: missing sport_slug or match_id")

        candidates_tpl = [
            "/v2/{sport}/matches/{id}",
            "/v2/{sport}/events/{id}",
            "/v2/{sport}/match/{id}",
            "/v2/{sport}/event/{id}",
            "/v2/{sport}/game/{id}",
        ]

        last_err: Optional[Exception] = None

        sports = [sport_slug] + [s for s in SPORT_ALIASES.get(sport_slug, []) if s != sport_slug]

        for s in sports:
            for tpl in candidates_tpl:
                path = tpl.format(sport=s, id=match_id)
                try:
                    data = await self._get_json(path)
                    obj = self._unwrap_obj(data)
                    if obj:
                        return self._match_to_dto(obj, sport_slug)
                except Exception as e:
                    last_err = e

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id or "").strip()
        if not sport_slug or not match_id:
            raise SportAPIError("match_odds: missing sport_slug or match_id")

        candidates_tpl = [
            "/v2/{sport}/matches/{id}/odds",
            "/v2/{sport}/events/{id}/odds",
            "/v2/{sport}/match/{id}/odds",
            "/v2/{sport}/event/{id}/odds",
            "/v2/{sport}/odds/{id}",
        ]

        last_err: Optional[Exception] = None

        sports = [sport_slug] + [s for s in SPORT_ALIASES.get(sport_slug, []) if s != sport_slug]

        for s in sports:
            for tpl in candidates_tpl:
                path = tpl.format(sport=s, id=match_id)
                try:
                    data = await self._get_json(path)
                    obj = self._unwrap_obj(data)
                    if obj:
                        return OddsSnapshot(raw=obj)
                except Exception as e:
                    last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")
