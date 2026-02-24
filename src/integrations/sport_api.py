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
    odds_base: Optional[Any] = None  # dict or list

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

    # 404s on these path patterns are expected (many leagues lack these endpoints)
    _SILENT_404_KEYWORDS = ("statistics", "lineups", "stats", "incidents", "events", "odds", "tennis")

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        timeout = httpx.Timeout(self.timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {}, headers=self.headers)

        if r.status_code >= 400:
            txt = (r.text or "")[:800]
            err = SportAPIError(f"HTTP {r.status_code}: {txt}")
            # Don't alert on expected 404s (statistics/lineups for leagues that lack them)
            is_silent = (
                r.status_code == 404
                and any(kw in path.lower() for kw in self._SILENT_404_KEYWORDS)
            )
            if not is_silent:
                try:
                    from ..alerting import send_alert
                    import asyncio
                    asyncio.ensure_future(send_alert(
                        "ERROR", "sport_api._get_json", err,
                        context={"url": url, "status": r.status_code},
                    ))
                except Exception:
                    pass
            raise err

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
            odds_base=odds_base if isinstance(odds_base, (dict, list)) else None,

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

    def _parse_espn_tennis_comps(
        self, comps: list, ev: dict, tour_name: str, tournament: str,
    ) -> List[MatchDTO]:
        """Extract tennis matches from an ESPN competitions array."""
        result: List[MatchDTO] = []
        for comp in comps:
            try:
                mid = str(comp.get("id") or ev.get("id") or "unknown")
                competitors = comp.get("competitors") or []
                if len(competitors) < 2:
                    continue

                names: List[str] = []
                scores: List[str] = []
                for c in competitors:
                    # Player name: athlete → direct fields → team
                    athlete = c.get("athlete") or {}
                    name = (
                        athlete.get("shortName")
                        or athlete.get("displayName")
                        or athlete.get("name")
                        or c.get("displayName")
                        or c.get("name")
                        or (c.get("team") or {}).get("name")
                        or "TBD"
                    )
                    names.append(name)

                    # Score: handle string, dict, number
                    score_raw = c.get("score")
                    if isinstance(score_raw, dict):
                        score_val = str(
                            score_raw.get("displayValue")
                            or score_raw.get("value")
                            or ""
                        )
                    elif score_raw is not None:
                        score_val = str(score_raw)
                    else:
                        score_val = ""
                    scores.append(score_val)

                # Status: try comp.status → ev.status
                status_obj = comp.get("status") or ev.get("status") or {}
                status_type = status_obj.get("type") or {}
                status_name = status_type.get("name") or ""
                status_map = {
                    "STATUS_SCHEDULED": "NS",
                    "STATUS_IN_PROGRESS": "LIVE",
                    "STATUS_FINAL": "FT",
                    "STATUS_POSTPONED": "PST",
                    "STATUS_CANCELED": "CANC",
                    "STATUS_RETIRED": "RET",
                    "STATUS_WALKOVER": "W/O",
                }
                status = status_map.get(status_name, status_name)

                start_time = comp.get("date") or ev.get("date") or ""
                score = (
                    f"{scores[0]}:{scores[1]}"
                    if len(scores) >= 2 and scores[0] and scores[1]
                    else ""
                )

                result.append(MatchDTO(
                    id=mid,
                    sport_slug="tennis",
                    title=f"{names[0]} — {names[1]}",
                    league=f"{tour_name}: {tournament}",
                    status=status,
                    start_time=start_time,
                    score=score,
                    country="International",
                    raw=comp,
                ))
            except Exception:
                continue
        return result

    async def _fetch_tennis_espn(self, day: date) -> List[MatchDTO]:
        """
        Fetch tennis matches from ESPN public API (ATP + WTA).
        Free, no key. 20-50 matches/day during tournaments.

        ESPN tennis structure varies:
        - events[].competitions[] — direct match objects (each comp = match)
        - events[].competitions[].events[].competitions[] — nested (tournament→section→match)
        """
        day_s = day.strftime("%Y%m%d")
        urls = [
            ("ATP", "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard"),
            ("WTA", "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard"),
        ]
        result: List[MatchDTO] = []

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                for tour_name, url in urls:
                    try:
                        resp = await client.get(url, params={"dates": day_s})
                        if resp.status_code != 200:
                            logger.warning("ESPN %s: HTTP %d", tour_name, resp.status_code)
                            continue

                        data = resp.json()
                        events = data.get("events") or []
                        logger.info("ESPN %s: %d events on %s", tour_name, len(events), day.isoformat())

                        for ev in events:
                            try:
                                tournament = ev.get("name") or ev.get("shortName") or tour_name
                                comps = ev.get("competitions") or []

                                # Strategy 1: direct — competitions[] are matches
                                matches_from = self._parse_espn_tennis_comps(
                                    comps, ev, tour_name, tournament,
                                )

                                # Strategy 2: nested — competitions[].events[].competitions[]
                                if not matches_from and comps:
                                    for comp in comps:
                                        nested_events = comp.get("events") or []
                                        for nev in nested_events:
                                            nested_comps = nev.get("competitions") or []
                                            ntournament = (
                                                nev.get("name")
                                                or nev.get("shortName")
                                                or tournament
                                            )
                                            matches_from.extend(
                                                self._parse_espn_tennis_comps(
                                                    nested_comps, nev, tour_name, ntournament,
                                                )
                                            )

                                # Strategy 3: event itself is a match (competitors at event level)
                                if not matches_from:
                                    ev_competitors = ev.get("competitors") or []
                                    if len(ev_competitors) >= 2:
                                        matches_from = self._parse_espn_tennis_comps(
                                            [ev], ev, tour_name, tournament,
                                        )

                                # Strategy 4: groupings — events[].groupings[].competitions[]
                                # ESPN tennis often nests matches under groupings (rounds/draws)
                                if not matches_from:
                                    groupings = ev.get("groupings") or []
                                    for grp in groupings:
                                        grp_comps = grp.get("competitions") or []
                                        grp_obj = grp.get("grouping") or grp
                                        grp_name = ""
                                        if isinstance(grp_obj, dict):
                                            grp_name = (
                                                grp_obj.get("displayName")
                                                or grp_obj.get("name")
                                                or ""
                                            )
                                        ntournament = (
                                            f"{tournament} - {grp_name}"
                                            if grp_name else tournament
                                        )
                                        matches_from.extend(
                                            self._parse_espn_tennis_comps(
                                                grp_comps, ev, tour_name, ntournament,
                                            )
                                        )
                                    if groupings and matches_from:
                                        logger.info(
                                            "ESPN %s: %d matches via groupings for '%s'",
                                            tour_name, len(matches_from), tournament,
                                        )

                                # Strategy 5: leagues[].events[] (another ESPN nesting)
                                if not matches_from:
                                    leagues = ev.get("leagues") or []
                                    for lg in leagues:
                                        lg_events = lg.get("events") or []
                                        for lev in lg_events:
                                            lev_comps = lev.get("competitions") or []
                                            lev_name = (
                                                lev.get("name")
                                                or lev.get("shortName")
                                                or tournament
                                            )
                                            matches_from.extend(
                                                self._parse_espn_tennis_comps(
                                                    lev_comps, lev, tour_name, lev_name,
                                                )
                                            )

                                if not matches_from:
                                    # Log detailed structure for debugging
                                    ev_keys = list(ev.keys())[:15]
                                    comp0_keys = []
                                    if comps:
                                        comp0_keys = list(comps[0].keys())[:15] if isinstance(comps[0], dict) else []
                                    logger.debug(
                                        "ESPN %s: 0 matches from event '%s' "
                                        "(comps=%d, ev_keys=%s, comp0_keys=%s, "
                                        "has_groupings=%s, has_leagues=%s)",
                                        tour_name, tournament, len(comps),
                                        ev_keys, comp0_keys,
                                        bool(ev.get("groupings")),
                                        bool(ev.get("leagues")),
                                    )

                                result.extend(matches_from)
                            except Exception:
                                logger.exception("ESPN %s: error parsing event", tour_name)
                                continue
                    except Exception:
                        logger.exception("ESPN %s: request failed", tour_name)
                        continue

            # Filter: only show NS (scheduled) and LIVE matches, not finished
            total_before = len(result)
            result = [m for m in result if m.status in ("NS", "LIVE", "HT", "")]
            logger.info("ESPN Tennis: total %d matches (ATP+WTA), %d after filtering (NS/LIVE only)",
                        total_before, len(result))
            return result

        except Exception:
            logger.exception("ESPN Tennis: request failed")
            return []

    async def _fetch_mma_espn(self, day: date) -> List[MatchDTO]:
        """
        Fetch UFC/MMA fights from ESPN public API.
        Free, no key. ~10-13 fights per event, events every 1-2 weeks.
        Returns fights from the nearest upcoming/current UFC card.
        """
        day_s = day.strftime("%Y%m%d")
        url = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
        params = {"dates": day_s}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, params=params)

                if resp.status_code != 200:
                    logger.warning("ESPN MMA: HTTP %d", resp.status_code)
                    return []

                data = resp.json()
                events = data.get("events") or []
                logger.info("ESPN MMA: %d events on %s", len(events), day.isoformat())

                result: List[MatchDTO] = []
                for ev in events:
                    try:
                        card_name = ev.get("name") or ev.get("shortName") or "UFC"

                        comps = ev.get("competitions") or []
                        for comp in comps:
                            mid = str(comp.get("id") or ev.get("id") or "unknown")

                            competitors = comp.get("competitors") or []
                            if len(competitors) < 2:
                                continue

                            names = []
                            for c in competitors:
                                athlete = c.get("athlete") or {}
                                name = (
                                    athlete.get("shortName")
                                    or athlete.get("displayName")
                                    or athlete.get("name")
                                    or "TBD"
                                )
                                names.append(name)

                            status_obj = comp.get("status") or {}
                            status_type = status_obj.get("type") or {}
                            status_name = status_type.get("name") or ""
                            status_map = {
                                "STATUS_SCHEDULED": "NS",
                                "STATUS_IN_PROGRESS": "LIVE",
                                "STATUS_FINAL": "FT",
                                "STATUS_POSTPONED": "PST",
                                "STATUS_CANCELED": "CANC",
                            }
                            status = status_map.get(status_name, status_name)

                            start_time = comp.get("date") or ev.get("date") or ""

                            # Card type from competition
                            card_type = ""
                            comp_type = comp.get("type") or {}
                            if isinstance(comp_type, dict):
                                card_type = comp_type.get("text") or comp_type.get("abbreviation") or ""
                            # Also try notes for weight class
                            notes = comp.get("notes") or []
                            weight_class = ""
                            if isinstance(notes, list) and notes:
                                for note in notes:
                                    if isinstance(note, dict):
                                        weight_class = note.get("text") or note.get("headline") or ""
                                        if weight_class:
                                            break
                                    elif isinstance(note, str):
                                        weight_class = note
                                        break

                            # Build title with card type prefix
                            card_prefix = ""
                            if card_type:
                                ct = card_type.lower()
                                if "main" in ct:
                                    card_prefix = "Main"
                                elif "prelim" in ct and "early" in ct:
                                    card_prefix = "Early"
                                elif "prelim" in ct:
                                    card_prefix = "Prelim"
                            fight_title = f"{names[0]} vs {names[1]}"
                            if card_prefix:
                                fight_title = f"{card_prefix} | {fight_title}"

                            # Winner info
                            score = ""
                            for c in competitors:
                                if c.get("winner"):
                                    athlete = c.get("athlete") or {}
                                    score = "W: " + (athlete.get("shortName") or "")
                                    break

                            result.append(MatchDTO(
                                id=mid,
                                sport_slug="mma",
                                title=fight_title,
                                league=card_name,
                                status=status,
                                start_time=start_time,
                                score=score,
                                country="USA",
                                raw=comp,
                            ))
                    except Exception:
                        continue

                # Sort: LIVE first, then NS (upcoming), then FT (finished)
                def _mma_sort_key(m: MatchDTO) -> int:
                    s = (m.status or "").upper()
                    if s == "LIVE":
                        return 0
                    if s in ("NS", ""):
                        return 1
                    return 2  # FT, PST, etc.
                result.sort(key=_mma_sort_key)

                # Filter out finished fights from the list (keep only NS/LIVE)
                total_before = len(result)
                result = [m for m in result if m.status in ("NS", "LIVE", "")]

                # ESPN MMA scoreboard returns nearest event even if it's weeks away.
                # Filter: only keep fights whose start_time is on the requested day.
                day_iso = day.isoformat()  # "2026-02-24"
                on_day = [m for m in result if day_iso in (m.start_time or "")]
                if on_day:
                    result = on_day
                else:
                    # No fights on this exact day — log the actual dates
                    dates_found = set()
                    for m in result:
                        st = (m.start_time or "")[:10]
                        if st:
                            dates_found.add(st)
                    logger.info(
                        "ESPN MMA: no fights on %s, nearest event dates: %s",
                        day_iso, sorted(dates_found),
                    )
                    result = []  # return empty — _render_sport_nav_root will show next event

                logger.info("ESPN MMA: parsed %d fights, %d on %s",
                            total_before, len(result), day_iso)
                return result

        except Exception:
            logger.exception("ESPN MMA: request failed")
            return []

    async def fetch_next_ufc_event(self) -> Optional[Dict[str, str]]:
        """Fetch the nearest upcoming UFC event from ESPN.

        Returns dict with 'name' and 'date' or None.
        Checks next 30 days from today.
        """
        from datetime import timedelta
        today = date.today()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                for offset in range(0, 30, 7):
                    check_day = today + timedelta(days=offset)
                    day_s = check_day.strftime("%Y%m%d")
                    url = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
                    resp = await client.get(url, params={"dates": day_s})
                    if resp.status_code != 200:
                        continue
                    events = resp.json().get("events") or []
                    for ev in events:
                        ev_date = ev.get("date") or ""
                        ev_name = ev.get("name") or ev.get("shortName") or "UFC"
                        # Check if event is in the future
                        if ev_date and ev_name:
                            return {"name": ev_name, "date": ev_date}
        except Exception:
            logger.debug("fetch_next_ufc_event failed")
        return None

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

        # ── Football: api-sports.io (paid $99 all-sports) ──
        if sport_slug == "football":
            try:
                # Log config for debugging
                try:
                    from ..sports_config import get_sport_config, get_leagues
                    _fcfg = get_sport_config("football")
                    _fleagues = get_leagues("football", max_priority=3)
                    _seasons = {lid: li.get("season") for lid, li in _fleagues.items()}
                    logger.info(
                        "Football config: api_base=%s match_param=%s seasons=%s",
                        (_fcfg or {}).get("api_base", "?"),
                        (_fcfg or {}).get("match_param", "?"),
                        _seasons,
                    )
                except Exception:
                    pass
                matches = await self._fallback_api_sports(sport_slug, day)
                logger.info("SportAPI football (api-sports.io): n=%d", len(matches))
                return matches
            except Exception as e:
                logger.warning("api-sports.io football failed: %s", e)
                last_err = e
            logger.info("football: no matches on %s (check API_SPORTS_KEY)", day_s)
            return []

        # ── Basketball: api-sports.io (NBA, Euroleague, VTB, Eurocup) ──
        if sport_slug == "basketball":
            try:
                matches = await self._fallback_api_sports(sport_slug, day)
                logger.info("SportAPI basketball (api-sports.io): n=%d", len(matches))
                return matches
            except Exception as e:
                logger.warning("api-sports.io basketball failed: %s", e)
                last_err = e
            logger.info("basketball: no matches on %s (check API_SPORTS_KEY)", day_s)
            return []

        # ── Tennis: ESPN public API (ATP + WTA, free, no key) ──
        if sport_slug == "tennis":
            try:
                tennis_matches = await self._fetch_tennis_espn(day)
                logger.info("SportAPI ESPN Tennis: n=%d", len(tennis_matches))
                return tennis_matches
            except Exception as e:
                logger.warning("ESPN Tennis failed: %s", e)
            return []

        # ── MMA/UFC: ESPN only (api-sports.io unreliable for MMA) ──
        if sport_slug == "mma":
            try:
                mma_matches = await self._fetch_mma_espn(day)
                if mma_matches:
                    logger.info("SportAPI ESPN MMA: n=%d", len(mma_matches))
                    return mma_matches
            except Exception:
                logger.debug("ESPN MMA failed for %s", day_s)

            logger.info("mma: no fights on %s", day_s)
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
                final_err = SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")
                try:
                    from ..alerting import send_alert
                    import asyncio
                    asyncio.ensure_future(send_alert(
                        "ERROR", "sport_api.matches_by_date", final_err,
                        context={"sport": sport_slug, "date": day_s},
                    ))
                except Exception:
                    pass
                raise final_err
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

    async def _fetch_tennis_espn_details(self, match_id: str) -> Optional[MatchDTO]:
        """Fetch single tennis match details from ESPN summary API."""
        for tour in ("atp", "wta"):
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/summary"
                params = {"event": match_id}

                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        continue

                    data = resp.json()

                    # Try header.competitions → event.competitions
                    competitions: list = []
                    header = data.get("header") or {}
                    if isinstance(header, dict):
                        competitions = header.get("competitions") or []
                    if not competitions:
                        event_obj = data.get("event") or {}
                        if isinstance(event_obj, dict):
                            competitions = event_obj.get("competitions") or []
                    if not competitions:
                        continue

                    comp = competitions[0]
                    competitors = comp.get("competitors") or []
                    if len(competitors) < 2:
                        continue

                    names: List[str] = []
                    scores: List[str] = []
                    for c in competitors:
                        athlete = c.get("athlete") or {}
                        name = (
                            athlete.get("shortName")
                            or athlete.get("displayName")
                            or c.get("displayName")
                            or "TBD"
                        )
                        names.append(name)
                        score_raw = c.get("score")
                        if isinstance(score_raw, dict):
                            scores.append(str(score_raw.get("displayValue") or score_raw.get("value") or ""))
                        else:
                            scores.append(str(score_raw) if score_raw is not None else "")

                    status_obj = comp.get("status") or {}
                    status_type = status_obj.get("type") or {}
                    status_name = status_type.get("name") or ""
                    status_map = {
                        "STATUS_SCHEDULED": "NS",
                        "STATUS_IN_PROGRESS": "LIVE",
                        "STATUS_FINAL": "FT",
                        "STATUS_POSTPONED": "PST",
                        "STATUS_CANCELED": "CANC",
                        "STATUS_RETIRED": "RET",
                        "STATUS_WALKOVER": "W/O",
                    }
                    status = status_map.get(status_name, status_name)

                    event_info = data.get("event") or data.get("header") or {}
                    tournament = ""
                    if isinstance(event_info, dict):
                        tournament = event_info.get("name") or ""

                    return MatchDTO(
                        id=match_id,
                        sport_slug="tennis",
                        title=f"{names[0]} — {names[1]}",
                        league=f"{tour.upper()}: {tournament}",
                        status=status,
                        start_time=comp.get("date") or "",
                        score=f"{scores[0]}:{scores[1]}" if scores[0] and scores[1] else "",
                        country="International",
                        raw=data,
                    )
            except Exception:
                logger.debug("ESPN Tennis details: %s tour=%s failed", match_id, tour)
                continue
        return None

    async def _fetch_mma_espn_details(self, match_id: str) -> Optional[MatchDTO]:
        """Fetch single MMA fight details from ESPN.

        MMA fights come from ESPN scoreboard with ESPN IDs.
        Try summary API first, then fall back to scoreboard search.
        """
        # Strategy 1: ESPN event summary
        for league in ("ufc",):
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/mma/{league}/event"
                params = {"event": match_id}

                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        data = resp.json()
                        # Try to parse competitions from the event
                        competitions = data.get("competitions") or []
                        if not competitions:
                            # Try nested structure
                            event_obj = data.get("event") or data
                            competitions = event_obj.get("competitions") or []
                        if not competitions:
                            header = data.get("header") or {}
                            competitions = header.get("competitions") or []

                        for comp in competitions:
                            if str(comp.get("id")) == str(match_id):
                                return self._parse_mma_espn_competition(comp, data)

                        # If event has only one competition, use it
                        if len(competitions) == 1:
                            return self._parse_mma_espn_competition(competitions[0], data)

            except Exception:
                logger.debug("ESPN MMA event API failed for %s", match_id)

        # Strategy 2: Search in today's scoreboard
        try:
            from datetime import date as _date
            today = _date.today()
            matches = await self._fetch_mma_espn(today)
            for m in matches:
                if str(m.id) == str(match_id):
                    logger.info("ESPN MMA match_details: found %s in scoreboard", match_id)
                    return m
        except Exception:
            logger.debug("ESPN MMA scoreboard search failed for %s", match_id)

        return None

    def _parse_mma_espn_competition(self, comp: dict, full_data: dict) -> Optional[MatchDTO]:
        """Parse a single MMA competition from ESPN data into MatchDTO."""
        try:
            mid = str(comp.get("id") or "unknown")
            competitors = comp.get("competitors") or []
            if len(competitors) < 2:
                return None

            names = []
            records = []
            for c in competitors:
                athlete = c.get("athlete") or {}
                name = (
                    athlete.get("shortName")
                    or athlete.get("displayName")
                    or athlete.get("name")
                    or "TBD"
                )
                names.append(name)
                record = athlete.get("record") or ""
                records.append(record)

            status_obj = comp.get("status") or {}
            status_type = status_obj.get("type") or {}
            status_name = status_type.get("name") or ""
            status_map = {
                "STATUS_SCHEDULED": "NS",
                "STATUS_IN_PROGRESS": "LIVE",
                "STATUS_FINAL": "FT",
                "STATUS_POSTPONED": "PST",
                "STATUS_CANCELED": "CANC",
            }
            status = status_map.get(status_name, status_name)

            start_time = comp.get("date") or ""

            # Card name from event
            card_name = ""
            if isinstance(full_data, dict):
                card_name = full_data.get("name") or full_data.get("shortName") or ""
                if not card_name:
                    header = full_data.get("header") or {}
                    card_name = header.get("name") or ""

            score = ""
            for c in competitors:
                if c.get("winner"):
                    athlete = c.get("athlete") or {}
                    score = "W: " + (athlete.get("shortName") or "")
                    break

            league = card_name or "UFC"

            return MatchDTO(
                id=mid,
                sport_slug="mma",
                title=f"{names[0]} vs {names[1]}",
                league=league,
                status=status,
                start_time=start_time,
                score=score,
                country="USA",
                raw=full_data,
            )
        except Exception:
            logger.exception("_parse_mma_espn_competition failed")
            return None

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id or "").strip()
        if not sport_slug or not match_id:
            raise SportAPIError("match_details: missing sport_slug or match_id")

        last_err: Optional[Exception] = None

        # ── Tennis: use ESPN (api-sports.io doesn't support tennis) ──
        if sport_slug == "tennis":
            try:
                dto = await self._fetch_tennis_espn_details(match_id)
                if dto:
                    return dto
            except Exception as e:
                last_err = e
                logger.debug("ESPN Tennis match_details failed for %s: %s", match_id, e)
            # Fallback: build minimal DTO from matches_by_date scoreboard cache
            try:
                from datetime import date as _date
                today = _date.today()
                matches = await self.matches_by_date("tennis", today)
                for m in matches:
                    if str(getattr(m, "id", "")) == str(match_id):
                        return m
            except Exception:
                logger.debug("Tennis scoreboard fallback also failed for %s", match_id)
            raise SportAPIError(f"match_details failed: tennis/{match_id}: {last_err}")

        # ── MMA: use ESPN (ESPN IDs don't exist in api-sports.io) ──
        if sport_slug == "mma":
            try:
                dto = await self._fetch_mma_espn_details(match_id)
                if dto:
                    return dto
            except Exception as e:
                last_err = e
                logger.warning("ESPN MMA match_details failed for %s: %s", match_id, e)

            # Fallback: try api-sports.io in case it's an api-sports ID
            try:
                dto = await self._fallback_match_details(sport_slug, match_id)
                if dto:
                    return dto
            except Exception as e:
                last_err = e

            raise SportAPIError(f"match_details failed: mma/{match_id}: {last_err}")

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

    # Cache: league_id → working season (avoids double requests on fallback)
    _league_season_cache: Dict[int, Any] = {}

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
                    cfg_season = league_info.get("season", 2025)

                    # Use cached season if we already know which one works
                    # Football: always use config season (year of season start, e.g. 2025 for 2025-26)
                    if sport_slug == "football":
                        season = cfg_season
                        cached_season = None
                    else:
                        cached_season = self._league_season_cache.get(league_id)
                        season = cached_season if cached_season is not None else cfg_season

                    params = {
                        "league": league_id,
                        "season": season,
                        "date": day_s,
                        "timezone": "Europe/Moscow",
                    }
                    logger.info(
                        "api-sports.io: GET %s league=%s season=%s date=%s%s",
                        url, league_id, season, day_s,
                        " (cached)" if cached_season is not None else "",
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
                    errors = data.get("errors")

                    # Season fallback: try season-1 if errors or 0 results
                    # SKIP for football — season is always correct (year of season start)
                    # Only do fallback if we haven't already cached a season
                    if (errors or not items) and cached_season is None and sport_slug != "football":
                        # Build fallback season
                        fb_season = None
                        try:
                            fb_season = int(cfg_season) - 1
                        except (ValueError, TypeError):
                            if isinstance(cfg_season, str) and "-" in cfg_season:
                                parts = cfg_season.split("-")
                                try:
                                    fb_season = f"{int(parts[0]) - 1}-{int(parts[1]) - 1}"
                                except (ValueError, IndexError):
                                    pass

                        if fb_season is not None:
                            logger.info(
                                "api-sports.io: league=%d season=%s → %s, trying season=%s",
                                league_id, season,
                                f"errors={errors}" if errors else "0 results",
                                fb_season,
                            )
                            params_fb = dict(params)
                            params_fb["season"] = fb_season
                            resp_fb = await client.get(url, headers=headers, params=params_fb)
                            if resp_fb.status_code == 200:
                                data_fb = resp_fb.json()
                                errors_fb = data_fb.get("errors")
                                items_fb = data_fb.get("response") or []
                                if not errors_fb and items_fb:
                                    items = items_fb
                                    # Cache the working season
                                    self._league_season_cache[league_id] = fb_season
                                    logger.info(
                                        "api-sports.io: league=%d season=%s fallback → %d matches (cached)",
                                        league_id, fb_season, len(items),
                                    )
                                elif not errors_fb and not items_fb:
                                    # Neither season has matches today — cache original to avoid retry
                                    self._league_season_cache[league_id] = season
                        elif errors:
                            logger.warning("api-sports.io: league=%d errors=%s", league_id, errors)
                    elif items and cached_season is None:
                        # First success — cache the season
                        self._league_season_cache[league_id] = season

                    logger.info(
                        "api-sports.io: league=%d (%s) → %d matches",
                        league_id, league_info.get("name", ""), len(items),
                    )

                    parsed_count = 0
                    for item in items:
                        try:
                            dto = self._parse_api_sports_match(item, sport_slug, league_info)
                            all_matches.append(dto)
                            parsed_count += 1
                        except Exception:
                            logger.debug(
                                "api-sports.io: parse error for item keys=%s",
                                list(item.keys())[:10] if isinstance(item, dict) else type(item).__name__,
                            )
                            continue

                    if items and parsed_count == 0:
                        # Log first item for debugging
                        first = items[0] if items else {}
                        logger.warning(
                            "api-sports.io: league=%d had %d items but 0 parsed. "
                            "first_item_keys=%s",
                            league_id, len(items),
                            list(first.keys())[:15] if isinstance(first, dict) else str(first)[:200],
                        )
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

    # ------------------------------------------------------------------
    # Formula 1 dedicated methods (races, not daily fixtures)
    # ------------------------------------------------------------------
    async def f1_races_calendar(self, season: int = 2026) -> List[Dict[str, Any]]:
        """Fetch F1 race calendar for a season."""
        api_key = _env("API_SPORTS_KEY")
        if not api_key:
            return []
        url = "https://v1.formula-1.api-sports.io/races"
        headers = {"x-apisports-key": api_key}
        params = {"season": season, "timezone": "Europe/Moscow", "type": "Race"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
                return data.get("response", [])
        except Exception:
            logger.exception("F1 calendar fetch failed")
            return []

    async def f1_driver_standings(self, season: int = 2026) -> List[Dict[str, Any]]:
        """Fetch F1 driver standings."""
        api_key = _env("API_SPORTS_KEY")
        if not api_key:
            return []
        url = "https://v1.formula-1.api-sports.io/rankings/drivers"
        headers = {"x-apisports-key": api_key}
        params = {"season": season}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
                return data.get("response", [])
        except Exception:
            logger.exception("F1 standings fetch failed")
            return []

    async def f1_team_standings(self, season: int = 2026) -> List[Dict[str, Any]]:
        """Fetch F1 constructor standings."""
        api_key = _env("API_SPORTS_KEY")
        if not api_key:
            return []
        url = "https://v1.formula-1.api-sports.io/rankings/teams"
        headers = {"x-apisports-key": api_key}
        params = {"season": season}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s)) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
                return data.get("response", [])
        except Exception:
            logger.exception("F1 team standings fetch failed")
            return []
