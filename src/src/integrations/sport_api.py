# src/integrations/sport_api.py
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx


class SportAPIError(Exception):
    pass


@dataclass
class MatchDTO:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str
    start_time: str
    score: str = ""                 # "2:1" / "3-2" etc (best effort)
    odds_base: Optional[Dict[str, Any]] = None
    country: str = ""


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
    return {hdr: val}


def _pick_ru_name(team_obj: Any, fallback: str) -> str:
    if isinstance(team_obj, dict):
        tr = team_obj.get("translations") or team_obj.get("translation") or {}
        if isinstance(tr, dict):
            ru = tr.get("ru")
            if ru:
                return str(ru)
        nm = team_obj.get("name")
        if nm:
            return str(nm)
    if isinstance(team_obj, str) and team_obj.strip():
        return team_obj.strip()
    return fallback


def _unwrap_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "response", "results", "items", "matches", "events"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _unwrap_obj(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        for k in ("data", "response", "result", "item", "match", "event"):
            v = data.get(k)
            if isinstance(v, dict):
                return v
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _fmt_score_from_raw(raw: Dict[str, Any]) -> str:
    """
    Best-effort score extraction across providers.
    Return "" if not found.
    """
    # direct string
    for k in ("score", "result", "ftScore", "finalScore"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # common numeric pairs: home/away
    pairs: List[Tuple[Any, Any]] = []

    # flat keys
    pairs.append((raw.get("homeScore"), raw.get("awayScore")))
    pairs.append((raw.get("home_score"), raw.get("away_score")))
    pairs.append((raw.get("home"), raw.get("away")))  # sometimes is numeric

    # nested: scores / score
    scores = raw.get("scores") or raw.get("score")
    if isinstance(scores, dict):
        # try common nesting
        for hk, ak in (("home", "away"), ("homeScore", "awayScore"), ("h", "a")):
            pairs.append((scores.get(hk), scores.get(ak)))
        # some have: {"1": {"home":..,"away":..}, "2": ...}
        ft = scores.get("ft") or scores.get("final") or scores.get("fulltime")
        if isinstance(ft, dict):
            pairs.append((ft.get("home"), ft.get("away")))

    # provider-like: {"results":{"home":x,"away":y}}
    res = raw.get("results")
    if isinstance(res, dict):
        pairs.append((res.get("home"), res.get("away")))

    def _to_int(x: Any) -> Optional[int]:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return int(x)
        if isinstance(x, str):
            m = re.search(r"-?\d+", x)
            if m:
                try:
                    return int(m.group(0))
                except Exception:
                    return None
        return None

    for h, a in pairs:
        hi = _to_int(h)
        ai = _to_int(a)
        if hi is not None and ai is not None:
            return f"{hi}:{ai}"

    # last resort: try parse from title-like fields
    txt = ""
    for k in ("name", "title"):
        v = raw.get(k)
        if isinstance(v, str):
            txt += " " + v
    txt = txt.strip()
    if txt:
        m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", txt)
        if m:
            return f"{m.group(1)}:{m.group(2)}"

    return ""


class SportAPIClient:
    """
    Client for api.api-sport.ru style endpoints (and similar).
    Required ENV:
      - SPORT_API_BASE (e.g. https://api.api-sport.ru)
      - SPORT_API_KEY (+ optional header/prefix)
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE", "").rstrip("/")
        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        self.headers = _auth_headers()

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {}, headers=self.headers)
        if r.status_code >= 400:
            raise SportAPIError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            raise SportAPIError(f"Bad JSON from API: {r.text[:200]}")

    def _match_to_dto(self, raw: Dict[str, Any], sport_slug: str) -> MatchDTO:
        mid = str(raw.get("id") or raw.get("eventId") or raw.get("matchId") or "")

        home = _pick_ru_name(raw.get("homeTeam") or raw.get("home_team") or raw.get("teamHome"), "Home")
        away = _pick_ru_name(raw.get("awayTeam") or raw.get("away_team") or raw.get("teamAway"), "Away")

        # some providers store as strings
        if home == "Home":
            home = str(raw.get("home") or "Home")
        if away == "Away":
            away = str(raw.get("away") or "Away")

        league = ""
        tournament = raw.get("tournament") or raw.get("tourney") or raw.get("competition")
        if isinstance(tournament, dict):
            league = (
                (tournament.get("translations") or {}).get("ru")
                or tournament.get("name")
                or ""
            )
        if not league:
            lg = raw.get("league")
            if isinstance(lg, dict):
                league = str(lg.get("name") or "")
            else:
                league = str(raw.get("leagueName") or "")

        country = ""
        cobj = raw.get("country") or (tournament.get("country") if isinstance(tournament, dict) else None)
        if isinstance(cobj, dict):
            country = str(cobj.get("name") or cobj.get("ru") or "")
        elif isinstance(cobj, str):
            country = cobj

        status = str(raw.get("status") or raw.get("state") or raw.get("matchStatus") or "")
        start_time = str(raw.get("dateEvent") or raw.get("startTime") or raw.get("start_date") or raw.get("startDate") or "")

        score = _fmt_score_from_raw(raw)

        title = f"{home} — {away}"
        return MatchDTO(
            id=mid or "unknown",
            sport_slug=sport_slug,
            title=title,
            league=league,
            status=status,
            start_time=start_time,
            score=score,
            odds_base=(raw.get("oddsBase") if isinstance(raw.get("oddsBase"), dict) else None),
            country=country,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        """
        For api.api-sport.ru we know this works (per your logs):
          /v2/<sport>/matches?date=YYYY-MM-DD
        But keep fallback candidates for other providers.
        """
        sport_slug = sport_slug.strip()
        day_s = day.isoformat()

        candidates = [
            (f"/v2/{sport_slug}/matches", {"date": day_s}),
            (f"/v2/{sport_slug}/events", {"date": day_s}),
            (f"/v2/{sport_slug}/events/date/{day_s}", None),
        ]

        last_err: Optional[Exception] = None
        for path, params in candidates:
            try:
                data = await self._get_json(path, params=params)
                items = _unwrap_list(data)
                if items:
                    return [self._match_to_dto(x, sport_slug) for x in items]
            except Exception as e:
                last_err = e

        raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        """
        IMPORTANT FIX:
        For api.api-sport.ru the working style is /v2/<sport>/matches/{id}
        (your logs show /v2/<sport>/{id} => 404).
        We'll try multiple candidates incl. legacy.
        """
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates = [
            f"/v2/{sport_slug}/matches/{match_id}",
            f"/v2/{sport_slug}/events/{match_id}",
            # legacy/back-compat (some older code used this)
            f"/v2/{sport_slug}/{match_id}",
            # query-style (rare)
            f"/v2/{sport_slug}/matches",
        ]

        last_err: Optional[Exception] = None

        for path in candidates:
            try:
                if path.endswith("/matches") and path.count("/") >= 3:
                    data = await self._get_json(path, params={"id": match_id})
                    obj = _unwrap_obj(data)
                else:
                    data = await self._get_json(path)
                    obj = _unwrap_obj(data)

                if obj:
                    return self._match_to_dto(obj, sport_slug)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates = [
            f"/v2/{sport_slug}/matches/{match_id}/odds",
            f"/v2/{sport_slug}/events/{match_id}/odds",
        ]

        last_err: Optional[Exception] = None
        for path in candidates:
            try:
                data = await self._get_json(path)
                obj = _unwrap_obj(data)
                if obj:
                    return OddsSnapshot(raw=obj)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")
