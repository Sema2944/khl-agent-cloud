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
    # Hockey
    "KHL": "Russia",
    "NHL": "USA",
    "AHL": "USA",
    "SHL": "Sweden",
    "Liiga": "Finland",
    "DEL": "Germany",
    "Extraliga": "Czech",
    "VHL": "Russia",
    "NCAA": "USA",
    # Football
    "Premier League": "England",
    "RPL": "Russia",
    "La Liga": "Spain",
    "Bundesliga": "Germany",
    "Serie A": "Italy",
    "Ligue 1": "France",
    "Champions League": "Europe",
    "Europa League": "Europe",
    # Basketball
    "NBA": "USA",
    "Euroleague": "Europe",
    "VTB": "Russia",
    # Volleyball
    "Superliga": "Russia",
    "CEV": "Europe",
    # MMA
    "UFC": "USA",
    # Handball
    "EHF": "Europe",
}

SPORT_ALIASES: Dict[str, List[str]] = {
    "ice-hockey": ["hockey"],
    "table-tennis": ["ping-pong", "table_tennis", "tabletennis"],
    "volleyball": ["volley"],
    "handball": [],
    "mma": ["ufc"],
    "american-football": ["nfl", "american_football"],
    "formula1": ["f1", "formula-1"],
    "baseball": [],
    "rugby": [],
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

    # NEW: чтобы видеть реально что пришло
    raw: Optional[Dict[str, Any]] = None
    extras: Optional[Dict[str, Any]] = None



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
        return _first_str(raw.get("name"), raw.get("country"), raw.get("title"), raw.get("code"))
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


def _dig(d: Any, *path: str) -> Any:
    """Безопасно достаём вложенное значение из dict по пути."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _extract_country(raw: Dict[str, Any], tournament: Any, league: str) -> str:
    # 1) top-level
    country = _unwrap_country(raw.get("country"))
    if not country:
        country = _first_str(
            raw.get("countryName"),
            raw.get("leagueCountry"),
            raw.get("country_name"),
            raw.get("countryCode"),
        )

    # 2) tournament/league/competition shapes
    if not country and isinstance(tournament, dict):
        # tournament.country
        country = _unwrap_country(tournament.get("country"))
        if not country:
            # tournament.category
            category = tournament.get("category")
            if isinstance(category, dict):
                country = _first_str(category.get("name"), category.get("country"), category.get("title"))
                if not country:
                    country = _unwrap_country(category.get("country"))

        # частые варианты
        if not country:
            country = _first_str(
                _dig(tournament, "country", "name"),
                _dig(tournament, "country", "title"),
                _dig(tournament, "category", "name"),
                _dig(tournament, "category", "country"),
                _dig(tournament, "category", "country", "name"),
            )

    # 3) ещё пару популярных мест
    if not country:
        country = _first_str(
            _dig(raw, "tournament", "country", "name"),
            _dig(raw, "tournament", "category", "name"),
            _dig(raw, "league", "country", "name"),
            _dig(raw, "competition", "country", "name"),
        )

    # 4) fallback по лиге
    if not country:
        country = _league_country_hint(league)

    return country or "Other"


def _score_num(x: Any) -> Optional[int]:
    """Пытаемся привести счёт к числу из разных форматов."""
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return int(x)
    if isinstance(x, str):
        s = x.strip()
        if s.isdigit():
            return int(s)
        return None
    if isinstance(x, dict):
        # часто бывает {"current": 0} или {"display": 0} или {"value": 0}
        for k in ("current", "display", "value", "total", "score"):
            v = x.get(k)
            n = _score_num(v)
            if n is not None:
                return n
        return None
    return None


def _extract_score(raw: Dict[str, Any]) -> str:
    # string score
    s = raw.get("score")
    if isinstance(s, str) and s.strip():
        return s.strip()

    # simple numeric fields
    hs = raw.get("homeScore") or raw.get("home_score") or raw.get("scoreHome") or raw.get("goalsHome")
    aw = raw.get("awayScore") or raw.get("away_score") or raw.get("scoreAway") or raw.get("goalsAway")
    hn = _score_num(hs)
    an = _score_num(aw)
    if hn is not None and an is not None:
        return f"{hn}:{an}"

    # nested objects
    for key in ("scores", "goals", "result"):
        obj = raw.get(key)
        if isinstance(obj, dict):
            h = obj.get("home") or obj.get("homeScore") or obj.get("h")
            a = obj.get("away") or obj.get("awayScore") or obj.get("a")
            hn = _score_num(h)
            an = _score_num(a)
            if hn is not None and an is not None:
                return f"{hn}:{an}"

    # score dict variants
    if isinstance(s, dict):
        # fullTime / current / etc.
        ft = s.get("fullTime")
        if isinstance(ft, dict):
            hn = _score_num(ft.get("home"))
            an = _score_num(ft.get("away"))
            if hn is not None and an is not None:
                return f"{hn}:{an}"

        hn = _score_num(s.get("home"))
        an = _score_num(s.get("away"))
        if hn is not None and an is not None:
            return f"{hn}:{an}"

    # sometimes: homeTeam/awayTeam include score
    ht = raw.get("homeTeam")
    at = raw.get("awayTeam")
    if isinstance(ht, dict) and isinstance(at, dict):
        hn = _score_num(ht.get("score"))
        an = _score_num(at.get("score"))
        if hn is not None and an is not None:
            return f"{hn}:{an}"

    return ""


class SportAPIClient:
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

            # NEW:
            raw=raw,        # ← вот это главное
            extras=None,
        )


    def _is_primary_api_sport(self, sport_slug: str) -> bool:
        """Check if primary API (SPORT_API_BASE) supports this sport.
        Currently api-sport.ru only works for ice-hockey."""
        return sport_slug in {"ice-hockey", "hockey"}

    async def _fetch_basketball_espn(self, day: date) -> List[MatchDTO]:
        """
        Fetch NBA games from ESPN public API (free, no key required).
        Covers: NBA regular season + playoffs (~5-15 games/day Oct-Jun).
        """
        day_s = day.strftime("%Y%m%d")  # ESPN uses YYYYMMDD
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        params = {"dates": day_s}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, params=params)

                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    logger.warning("ESPN NBA: HTTP %d: %s", resp.status_code, body)
                    return []

                data = resp.json()
                events = data.get("events") or []
                logger.info("ESPN NBA: %d events on %s", len(events), day.isoformat())

                result: List[MatchDTO] = []
                for ev in events:
                    try:
                        mid = str(ev.get("id", "unknown"))
                        comps = ev.get("competitions") or []
                        if not comps:
                            continue
                        comp = comps[0]

                        competitors = comp.get("competitors") or []
                        home = away = "TBD"
                        home_score = away_score = None
                        for c in competitors:
                            team = c.get("team") or {}
                            name = (
                                team.get("shortDisplayName")
                                or team.get("displayName")
                                or team.get("name")
                                or "TBD"
                            )
                            if c.get("homeAway") == "home":
                                home = name
                                home_score = c.get("score")
                            else:
                                away = name
                                away_score = c.get("score")

                        # Status
                        status_obj = comp.get("status") or {}
                        status_type = status_obj.get("type") or {}
                        status_name = status_type.get("name") or ""
                        status_map = {
                            "STATUS_SCHEDULED": "NS",
                            "STATUS_IN_PROGRESS": "LIVE",
                            "STATUS_HALFTIME": "HT",
                            "STATUS_FINAL": "FT",
                            "STATUS_POSTPONED": "PST",
                            "STATUS_CANCELED": "CANC",
                            "STATUS_END_PERIOD": "LIVE",
                        }
                        status = status_map.get(status_name, status_name)

                        start_time = ev.get("date") or ""

                        score = ""
                        if home_score is not None and away_score is not None:
                            score = f"{home_score}:{away_score}"

                        result.append(MatchDTO(
                            id=mid,
                            sport_slug="basketball",
                            title=f"{home} — {away}",
                            league="NBA",
                            status=status,
                            start_time=start_time,
                            score=score,
                            country="USA",
                            raw=ev,
                        ))
                    except Exception:
                        continue

                logger.info("ESPN NBA: parsed %d matches", len(result))
                return result

        except Exception:
            logger.exception("ESPN NBA: request failed")
            return []

    async def _fetch_football_data_org(self, day: date) -> List[MatchDTO]:
        """
        Fetch football matches from football-data.org (free tier).
        Covers: PL, BL1, PD, SA, FL1, CL, ELC, DED, PPL.
        Free: 10 req/min, current season included.
        """
        api_key = _env("FOOTBALL_DATA_KEY")
        if not api_key:
            logger.warning("football-data.org: FOOTBALL_DATA_KEY not set")
            return []

        day_s = day.isoformat()
        url = "https://api.football-data.org/v4/matches"
        headers = {"X-Auth-Token": api_key}
        params = {"dateFrom": day_s, "dateTo": day_s}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, headers=headers, params=params)

                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    logger.warning("football-data.org: HTTP %d: %s", resp.status_code, body)
                    return []

                data = resp.json()
                matches_raw = data.get("matches") or []
                logger.info("football-data.org: %d matches on %s", len(matches_raw), day_s)

                result: List[MatchDTO] = []
                for m in matches_raw:
                    try:
                        mid = str(m.get("id", "unknown"))
                        home_obj = m.get("homeTeam") or {}
                        away_obj = m.get("awayTeam") or {}
                        home = home_obj.get("shortName") or home_obj.get("name") or "Home"
                        away = away_obj.get("shortName") or away_obj.get("name") or "Away"

                        comp = m.get("competition") or {}
                        league = comp.get("name") or ""
                        country = (comp.get("area") or {}).get("name") or ""

                        status_raw = (m.get("status") or "").upper()
                        status_map = {
                            "SCHEDULED": "NS", "TIMED": "NS", "IN_PLAY": "LIVE",
                            "PAUSED": "HT", "FINISHED": "FT", "POSTPONED": "PST",
                            "CANCELLED": "CANC", "SUSPENDED": "SUSP",
                        }
                        status = status_map.get(status_raw, status_raw)

                        start_time = m.get("utcDate") or ""

                        score_obj = m.get("score") or {}
                        ft = score_obj.get("fullTime") or {}
                        score = ""
                        if ft.get("home") is not None and ft.get("away") is not None:
                            score = f"{ft['home']}:{ft['away']}"

                        result.append(MatchDTO(
                            id=mid,
                            sport_slug="football",
                            title=f"{home} — {away}",
                            league=league,
                            status=status,
                            start_time=start_time,
                            score=score,
                            country=country,
                            raw=m,
                        ))
                    except Exception:
                        continue

                logger.info("football-data.org: parsed %d matches", len(result))
                return result

        except Exception:
            logger.exception("football-data.org: request failed")
            return []

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        sport_slug = (sport_slug or "").strip().lower()
        if not sport_slug:
            raise SportAPIError("matches_by_date: sport_slug is empty")

        day_s = day.isoformat()
        last_err: Optional[Exception] = None

        # ── Basketball: use ESPN public API (free, no key) ──
        if sport_slug == "basketball":
            try:
                espn_matches = await self._fetch_basketball_espn(day)
                logger.info("SportAPI ESPN NBA: n=%d", len(espn_matches))
                return espn_matches  # even 0 is OK — no games on some days
            except Exception as e:
                logger.warning("ESPN NBA failed: %s", e)
                last_err = e

            # Fallback to api-sports.io
            try:
                fallback_matches = await self._fallback_api_sports(sport_slug, day)
                if fallback_matches:
                    return fallback_matches
            except Exception as fb_err:
                last_err = fb_err

            logger.info("basketball: no matches on %s (normal for off-days)", day_s)
            return []

        # ── Football: use football-data.org (free, current season) ──
        if sport_slug == "football":
            fd_key = _env("FOOTBALL_DATA_KEY")
            if fd_key:
                try:
                    fd_matches = await self._fetch_football_data_org(day)
                    # football-data.org succeeded (even if 0 matches) → return result
                    # 0 matches is normal on days without games (weekdays)
                    logger.info("SportAPI football-data.org: n=%d", len(fd_matches))
                    return fd_matches
                except Exception as e:
                    logger.warning("football-data.org failed: %s", e)
                    last_err = e

            # Fallback to api-sports.io only if football-data.org key missing or errored
            try:
                fallback_matches = await self._fallback_api_sports(sport_slug, day)
                if fallback_matches:
                    return fallback_matches
            except Exception as fb_err:
                last_err = fb_err

            # If we get here with no matches, return empty list (not an error)
            logger.info("football: no matches on %s (normal for non-match days)", day_s)
            return []

        # ── For other non-hockey sports: go to api-sports.io ──
        if not self._is_primary_api_sport(sport_slug):
            try:
                fallback_matches = await self._fallback_api_sports(sport_slug, day)
                if fallback_matches:
                    logger.info(
                        "SportAPI api-sports.io OK: sport=%s n=%d",
                        sport_slug, len(fallback_matches),
                    )
                    return fallback_matches
                else:
                    logger.warning("SportAPI api-sports.io returned 0 matches for %s %s", sport_slug, day_s)
            except Exception as fb_err:
                logger.exception("SportAPI api-sports.io failed for %s: %s", sport_slug, fb_err)
                last_err = fb_err

            if last_err:
                raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")
            raise SportAPIError(f"matches_by_date: no matches for {sport_slug} on {day_s} (check API_SPORTS_KEY)")

        # ── Hockey: try primary API first (api-sport.ru) ──
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
                        return out
                except Exception as e:
                    last_err = e

        # ── Fallback for hockey too: api-sports.io ──
        try:
            fallback_matches = await self._fallback_api_sports(sport_slug, day)
            if fallback_matches:
                logger.info(
                    "SportAPI fallback api-sports.io OK: sport=%s n=%d",
                    sport_slug, len(fallback_matches),
                )
                return fallback_matches
        except Exception as fb_err:
            logger.debug("SportAPI fallback failed for %s: %s", sport_slug, fb_err)

        if last_err:
            raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")

        raise SportAPIError(f"matches_by_date empty for {sport_slug} {day_s}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id or "").strip()
        if not sport_slug or not match_id:
            raise SportAPIError("match_details: missing sport_slug or match_id")

        last_err: Optional[Exception] = None

        # ── For non-hockey sports: use api-sports.io directly ──
        if not self._is_primary_api_sport(sport_slug):
            try:
                dto = await self._fallback_match_details(sport_slug, match_id)
                if dto:
                    return dto
            except Exception as e:
                last_err = e
                logger.warning("api-sports.io match_details failed for %s/%s: %s", sport_slug, match_id, e)

            if last_err:
                raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")
            raise SportAPIError(f"match_details: not found {sport_slug}/{match_id}")

        # ── Hockey: try primary API ──
        candidates_tpl = [
            "/v2/{sport}/matches/{id}",
            "/v2/{sport}/events/{id}",
            "/v2/{sport}/match/{id}",
            "/v2/{sport}/event/{id}",
            "/v2/{sport}/game/{id}",
        ]

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

        # ── Fallback for hockey too ──
        try:
            dto = await self._fallback_match_details(sport_slug, match_id)
            if dto:
                return dto
        except Exception:
            pass

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_extras(self, sport_slug: str, match_id: str) -> Dict[str, Any]:
        """
        Пытаемся вытащить лайв-статистику/ивенты разными путями.
        Ничего не ломаем: возвращаем dict со всеми успешными источниками.
        """
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id or "").strip()
        if not sport_slug or not match_id:
            raise SportAPIError("match_extras: missing sport_slug or match_id")

        candidates_tpl = [
            "/v2/{sport}/matches/{id}/statistics",
            "/v2/{sport}/matches/{id}/stats",
            "/v2/{sport}/matches/{id}/events",
            "/v2/{sport}/events/{id}/statistics",
            "/v2/{sport}/events/{id}/incidents",
            "/v2/{sport}/match/{id}/statistics",
            "/v2/{sport}/event/{id}/statistics",
            "/v2/{sport}/game/{id}/statistics",
        ]

        sports = [sport_slug] + [s for s in SPORT_ALIASES.get(sport_slug, []) if s != sport_slug]

        out: Dict[str, Any] = {}
        last_err: Optional[Exception] = None

        for s in sports:
            for tpl in candidates_tpl:
                path = tpl.format(sport=s, id=match_id)
                try:
                    data = await self._get_json(path)
                    # сохраняем как есть, чтобы видеть структуру провайдера
                    out[path] = data
                except Exception as e:
                    last_err = e

        return {
            "sources": out,
            "last_error": str(last_err) if last_err else "",
        }


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

    async def _fallback_api_sports(self, sport_slug: str, day: date) -> List[MatchDTO]:
        """
        Fetch matches from api-sports.io.
        For non-hockey sports this is the PRIMARY source.
        Uses sports_config for correct endpoints/leagues per sport.
        """
        try:
            from ..sports_config import get_sport_config, get_leagues
        except ImportError:
            logger.warning("api-sports.io fallback: sports_config not available")
            return []

        cfg = get_sport_config(sport_slug)
        if not cfg:
            logger.warning("api-sports.io: no config for sport=%s", sport_slug)
            return []

        api_key = _env("API_SPORTS_KEY")
        if not api_key:
            logger.warning("api-sports.io: API_SPORTS_KEY not set")
            return []

        api_base = cfg.get("api_base", "")
        if not api_base:
            logger.warning("api-sports.io: no api_base for sport=%s", sport_slug)
            return []

        endpoints = cfg.get("endpoints", {})
        fixtures_ep = endpoints.get("fixtures", "/games")
        # Use all configured leagues (not just priority <= 2)
        leagues = get_leagues(sport_slug, max_priority=3)

        if not leagues:
            logger.warning("api-sports.io: no leagues configured for sport=%s", sport_slug)
            return []

        headers = {"x-apisports-key": api_key}
        day_s = day.isoformat()
        all_matches: List[MatchDTO] = []

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
            for league_id, league_info in leagues.items():
                try:
                    url = f"{api_base}{fixtures_ep}"
                    params = {
                        "league": league_id,
                        "season": league_info.get("season", 2025),
                        "date": day_s,
                        "timezone": "Europe/Moscow",
                    }
                    logger.info(
                        "api-sports.io: GET %s league=%s season=%s date=%s",
                        url, league_id, params["season"], day_s,
                    )
                    resp = await client.get(url, headers=headers, params=params)

                    # Log remaining requests from response headers
                    remaining = resp.headers.get("x-ratelimit-requests-remaining", "?")
                    logger.info(
                        "api-sports.io: league=%d HTTP %d, remaining_requests=%s",
                        league_id, resp.status_code, remaining,
                    )

                    if resp.status_code != 200:
                        body = (resp.text or "")[:300]
                        logger.warning(
                            "api-sports.io: HTTP %d for league=%d sport=%s: %s",
                            resp.status_code, league_id, sport_slug, body,
                        )
                        continue

                    data = resp.json()
                    items = data.get("response") or []
                    # Log errors array if present (api-sports.io sends errors here)
                    errors = data.get("errors")
                    if errors:
                        logger.warning(
                            "api-sports.io: league=%d errors=%s",
                            league_id, errors,
                        )
                        # Free plan fallback: retry with season 2024
                        err_msg = str(errors)
                        if "Free plans" in err_msg and "season" in err_msg:
                            logger.info(
                                "api-sports.io: retrying league=%d with season=2024 (free plan fallback)",
                                league_id,
                            )
                            params2 = dict(params)
                            params2["season"] = 2024
                            resp2 = await client.get(url, headers=headers, params=params2)
                            if resp2.status_code == 200:
                                data2 = resp2.json()
                                errors2 = data2.get("errors")
                                if not errors2:
                                    items = data2.get("response") or []
                                    logger.info(
                                        "api-sports.io: league=%d season=2024 fallback → %d matches",
                                        league_id, len(items),
                                    )

                    results_count = data.get("results", 0)
                    logger.info(
                        "api-sports.io: league=%d (%s) → %d matches (results=%s)",
                        league_id, league_info.get("name", ""), len(items), results_count,
                    )

                    for item in items:
                        try:
                            dto = self._parse_api_sports_match(item, sport_slug, league_info)
                            all_matches.append(dto)
                        except Exception:
                            continue
                except Exception:
                    logger.exception("api-sports.io: error fetching league=%d sport=%s", league_id, sport_slug)
                    continue

        if all_matches:
            logger.info("api-sports.io: total %d matches for %s %s", len(all_matches), sport_slug, day_s)
        else:
            logger.warning(
                "api-sports.io: 0 total matches for %s on %s. "
                "Possible causes: free tier limits, wrong season, or no games scheduled.",
                sport_slug, day_s,
            )
        return all_matches

    async def _fallback_match_details(self, sport_slug: str, match_id: str) -> Optional[MatchDTO]:
        """Fetch single match details from api-sports.io."""
        try:
            from ..sports_config import get_sport_config
        except ImportError:
            return None

        cfg = get_sport_config(sport_slug)
        if not cfg:
            return None

        api_key = _env("API_SPORTS_KEY")
        if not api_key:
            return None

        api_base = cfg.get("api_base", "")
        endpoints = cfg.get("endpoints", {})
        fixtures_ep = endpoints.get("fixtures", "/games")
        match_param = cfg.get("match_param", "id")

        headers = {"x-apisports-key": api_key}
        url = f"{api_base}{fixtures_ep}"
        params = {match_param: match_id}

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data.get("response") or []
            if not items:
                return None

            item = items[0]
            # Find league_info from config
            league_obj = item.get("league") or {}
            league_id = league_obj.get("id") if isinstance(league_obj, dict) else None
            all_leagues = cfg.get("leagues", {})
            league_info = all_leagues.get(league_id, {"name": "", "country": ""})

            return self._parse_api_sports_match(item, sport_slug, league_info)

    def _parse_api_sports_match(
        self, raw: Dict[str, Any], sport_slug: str, league_info: Dict[str, Any]
    ) -> MatchDTO:
        """Parse a single match from api-sports.io response into MatchDTO."""
        teams = raw.get("teams") or {}
        scores = raw.get("scores") or raw.get("goals") or {}
        status_obj = raw.get("status") or {}

        # Match ID
        mid = str(raw.get("id") or raw.get("fixture", {}).get("id") or "unknown")

        # For football, fixture is nested
        fixture = raw.get("fixture") or {}
        if fixture:
            mid = str(fixture.get("id") or mid)

        # Teams
        home_obj = teams.get("home") or {}
        away_obj = teams.get("away") or {}
        home = home_obj.get("name", "Home") if isinstance(home_obj, dict) else str(home_obj)
        away = away_obj.get("name", "Away") if isinstance(away_obj, dict) else str(away_obj)

        # League
        league_obj = raw.get("league") or {}
        league_name = league_info.get("name", "")
        if isinstance(league_obj, dict):
            league_name = league_obj.get("name") or league_name

        country = league_info.get("country", "Other")

        # Status
        if isinstance(status_obj, dict):
            status = status_obj.get("short") or status_obj.get("long") or ""
        else:
            status = str(status_obj or "")

        # Football has nested fixture.status
        if fixture and "status" in fixture:
            fs = fixture["status"]
            if isinstance(fs, dict):
                status = fs.get("short") or fs.get("long") or status

        # Start time
        start_time = str(
            raw.get("date") or raw.get("timestamp") or
            fixture.get("date") or fixture.get("timestamp") or ""
        )

        # Score
        score = ""
        if isinstance(scores, dict):
            h_s = scores.get("home")
            a_s = scores.get("away")
            if h_s is not None and a_s is not None:
                score = f"{h_s}:{a_s}"

        title = f"{home} — {away}"
        return MatchDTO(
            id=mid,
            sport_slug=sport_slug,
            title=title,
            league=league_name,
            status=status,
            start_time=start_time,
            score=score,
            country=country,
            raw=raw,
        )
