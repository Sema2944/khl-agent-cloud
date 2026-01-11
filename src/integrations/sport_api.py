# src/integrations/sport_api.py
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import httpx
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SportAPIError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    path = (path or "").lstrip("/")
    return f"{base}/{path}"


@dataclass
class MatchItem:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str
    start_time: str  # как приходит от провайдера (обычно ISO)


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


# алиасы: то, что приходит из твоего бота -> то, что может ожидать API
SPORT_SLUG_ALIASES: Dict[str, List[str]] = {
    "ice-hockey": ["hockey", "icehockey", "ice_hockey", "hokkey", "nhl"],
    "hockey": ["ice-hockey", "icehockey", "ice_hockey", "nhl"],
    "football": ["soccer", "football"],
    "soccer": ["football", "soccer"],
    "basketball": ["basketball"],
    "tennis": ["tennis"],
    "baseball": ["baseball"],
    "volleyball": ["volleyball"],
    "handball": ["handball"],
}


def _uniq(seq: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in seq:
        x = (x or "").strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


class SportAPIClient:
    """
    Универсальный клиент под Sport Events API.

    ENV:
      SPORT_API_BASE            например: https://api.api-sport.ru
      SPORT_API_KEY             ключ
      SPORT_API_KEY_HEADER      например: Authorization или X-Api-Key
      SPORT_API_KEY_PREFIX      например: Bearer   (если используешь Authorization: Bearer <key>)
      SPORT_API_TIMEOUT_S       по умолчанию 12
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        key_header: Optional[str] = None,
        key_prefix: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self.base = (base_url or _env("SPORT_API_BASE")).strip()
        self.key = (api_key or _env("SPORT_API_KEY")).strip()
        self.key_header = (key_header or _env("SPORT_API_KEY_HEADER", "Authorization")).strip()
        self.key_prefix = (key_prefix or _env("SPORT_API_KEY_PREFIX", "")).strip()
        self.timeout_s = float(timeout_s if timeout_s is not None else (_env("SPORT_API_TIMEOUT_S", "12") or 12))

        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        if not self.key:
            raise SportAPIError("SPORT_API_KEY is missing")

        u = urlparse(self.base)
        logger.info(
            "SportAPI init: base=%r scheme=%r host=%r header=%r prefix=%r timeout=%.1f key_present=%s",
            self.base,
            u.scheme,
            u.netloc,
            self.key_header,
            self.key_prefix,
            self.timeout_s,
            bool(self.key),
        )

    def _headers(self) -> Dict[str, str]:
        v = self.key
        if self.key_prefix:
            v = f"{self.key_prefix} {self.key}".strip()
        return {"Accept": "application/json", self.key_header: v}

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = _join_url(self.base, path)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                r = await client.get(url, headers=self._headers(), params=params)
            except Exception as e:
                raise SportAPIError(f"request failed: {type(e).__name__}: {e}") from e

        # 4xx/5xx
        if r.status_code >= 400:
            txt = (r.text or "")[:500]
            raise SportAPIError(f"HTTP {r.status_code}: {txt}")

        try:
            return r.json()
        except Exception as e:
            raise SportAPIError(f"bad json: {type(e).__name__}: {e}") from e

    # ---------- helpers: parsing разных форматов ----------
    def _pick(self, obj: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        return default

    def _stringify_id(self, x: Any) -> str:
        return "" if x is None else str(x)

    def _infer_title(self, m: Dict[str, Any]) -> str:
        t = self._pick(m, ["title", "name", "eventName", "matchName"], "")
        if t:
            return str(t)

        home = self._pick(m, ["homeName", "home_team", "homeTeam", "home", "team1"], "")
        away = self._pick(m, ["awayName", "away_team", "awayTeam", "away", "team2"], "")
        if home and away:
            return f"{home} — {away}"

        comps = m.get("competitors") or m.get("participants") or m.get("teams")
        if isinstance(comps, list) and len(comps) >= 2:
            a = comps[0].get("name") if isinstance(comps[0], dict) else str(comps[0])
            b = comps[1].get("name") if isinstance(comps[1], dict) else str(comps[1])
            if a and b:
                return f"{a} — {b}"

        return "Матч"

    def _infer_league(self, m: Dict[str, Any]) -> str:
        league = self._pick(m, ["league", "tournament", "competitionName", "competition", "leagueName"], "")
        if isinstance(league, dict):
            return str(self._pick(league, ["name", "title"], ""))
        return str(league or "")

    def _infer_status(self, m: Dict[str, Any]) -> str:
        s = self._pick(m, ["status", "state", "matchStatus", "eventStatus"], "")
        if isinstance(s, dict):
            return str(self._pick(s, ["type", "code", "name"], ""))
        return str(s or "")

    def _infer_start_time(self, m: Dict[str, Any]) -> str:
        st = self._pick(m, ["start_time", "startTime", "utcStartTime", "start", "date", "scheduled"], "")
        if isinstance(st, dict):
            return str(self._pick(st, ["utc", "iso", "dateTime"], ""))
        return str(st or "")

    def _extract_list(self, data: Any) -> Optional[List[Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "events", "matches", "results", "items"):
                if isinstance(data.get(k), list):
                    return data[k]
        return None

    def _looks_like_no_such_sport(self, err: Exception) -> bool:
        msg = str(err).lower()
        return ("no such sport endpoint" in msg) or ("no such sport" in msg)

    def _sport_candidates(self, sport_slug: str) -> List[str]:
        s = (sport_slug or "").strip().lower()
        cands = [s]
        cands += SPORT_SLUG_ALIASES.get(s, [])
        # доп. эвристики
        if s == "ice-hockey":
            cands += ["hockey"]
        if "-" in s:
            cands.append(s.replace("-", ""))
            cands.append(s.replace("-", "_"))
        return _uniq(cands)

    def _matches_paths(self, sport: str) -> List[str]:
        # пробуем разные варианты — у провайдеров часто отличаются
        # (важно: без лидирующего /, _join_url сам склеит)
        return [
            f"v2/{sport}/",
            f"v2/{sport}",
            f"v2/{sport}/events",
            f"v2/{sport}/matches",
            f"v2/{sport}/games",
            f"v2/{sport}/list",
        ]

    def _details_paths(self, sport: str, match_id: str) -> List[str]:
        return [
            f"v2/{sport}/{match_id}",
            f"v2/{sport}/events/{match_id}",
            f"v2/{sport}/matches/{match_id}",
            f"v2/{sport}/games/{match_id}",
        ]

    def _odds_paths(self, sport: str, match_id: str) -> List[str]:
        return [
            f"v2/{sport}/{match_id}/odds",
            f"v2/{sport}/{match_id}/markets",
            f"v2/{sport}/{match_id}/line",
            f"v2/{sport}/odds/{match_id}",
        ]

    async def _try_many_get(
        self,
        paths: List[str],
        params: Optional[Dict[str, Any]] = None,
        purpose: str = "",
    ) -> Tuple[Any, str]:
        last_err: Optional[Exception] = None
        for p in paths:
            try:
                logger.info("SportAPI try %s: GET %s params=%s", purpose or "request", p, params)
                data = await self._get(p, params=params)
                return data, p
            except Exception as e:
                last_err = e
                logger.warning("SportAPI failed %s on path=%s err=%s", purpose or "request", p, e)
        raise SportAPIError(f"all endpoints failed for {purpose}: {last_err}")

    # ---------- public API used by parsing.py ----------
    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchItem]:
        sport_slug = (sport_slug or "").strip().lower()
        params = {
            "date": day.isoformat(),
            "day": day.isoformat(),
            "from": day.isoformat(),
            "to": day.isoformat(),
            # доп. варианты
            "dateFrom": day.isoformat(),
            "dateTo": day.isoformat(),
            "startDate": day.isoformat(),
            "endDate": day.isoformat(),
        }

        candidates = self._sport_candidates(sport_slug)

        last_err: Optional[Exception] = None
        for sport in candidates:
            paths = self._matches_paths(sport)
            try:
                data, used = await self._try_many_get(paths, params=params, purpose=f"matches_by_date sport={sport}")
                items = self._extract_list(data)
                if not isinstance(items, list):
                    raise SportAPIError(f"unexpected response shape: {type(data).__name__}")
                out: List[MatchItem] = []
                for m in items:
                    if not isinstance(m, dict):
                        continue
                    mid = self._stringify_id(self._pick(m, ["id", "eventId", "matchId"], ""))
                    if not mid:
                        continue
                    out.append(
                        MatchItem(
                            id=mid,
                            sport_slug=sport,  # ВАЖНО: фактический sport, который сработал
                            title=self._infer_title(m),
                            league=self._infer_league(m),
                            status=self._infer_status(m),
                            start_time=self._infer_start_time(m),
                        )
                    )
                logger.info("SportAPI matches_by_date OK: requested=%s used_sport=%s used_path=%s n=%d", sport_slug, sport, used, len(out))
                return out
            except Exception as e:
                last_err = e
                # если это именно "не тот sport endpoint" — пробуем следующий алиас спорта
                if self._looks_like_no_such_sport(e):
                    continue
                # иначе (например 401/403/500) — тоже пробуем алиасы, но пусть будет шанс
                continue

        raise SportAPIError(f"matches_by_date failed for sport_slug={sport_slug}: {last_err}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchItem:
        match_id = str(match_id).strip()
        candidates = self._sport_candidates((sport_slug or "").strip().lower())

        last_err: Optional[Exception] = None
        for sport in candidates:
            paths = self._details_paths(sport, match_id)
            try:
                data, used = await self._try_many_get(paths, purpose=f"match_details sport={sport}")
                if isinstance(data, dict):
                    m = data.get("data") if isinstance(data.get("data"), dict) else data
                    if not isinstance(m, dict):
                        m = data
                else:
                    raise SportAPIError(f"unexpected response shape: {type(data).__name__}")

                logger.info("SportAPI match_details OK: requested=%s used_sport=%s used_path=%s", sport_slug, sport, used)
                return MatchItem(
                    id=match_id,
                    sport_slug=sport,
                    title=self._infer_title(m),
                    league=self._infer_league(m),
                    status=self._infer_status(m),
                    start_time=self._infer_start_time(m),
                )
            except Exception as e:
                last_err = e
                if self._looks_like_no_such_sport(e):
                    continue
                continue

        raise SportAPIError(f"match_details failed for sport_slug={sport_slug} match_id={match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        match_id = str(match_id).strip()
        candidates = self._sport_candidates((sport_slug or "").strip().lower())

        last_err: Optional[Exception] = None
        for sport in candidates:
            paths = self._odds_paths(sport, match_id)
            try:
                data, used_path = await self._try_many_get(paths, purpose=f"match_odds sport={sport}")

                raw: Dict[str, Any]
                if isinstance(data, dict):
                    raw = data
                else:
                    raw = {"data": data}

                moneyline = None
                total_main = None
                handicap_main = None

                root = data
                if isinstance(data, dict):
                    for k in ("data", "odds", "markets", "result"):
                        if k in data:
                            root = data[k]
                            break

                markets = None
                if isinstance(root, list):
                    markets = root
                elif isinstance(root, dict):
                    for k in ("markets", "items", "data", "lines"):
                        if isinstance(root.get(k), list):
                            markets = root[k]
                            break

                if isinstance(markets, list):

                    def mname(x: Dict[str, Any]) -> str:
                        n = x.get("name") or x.get("key") or x.get("type") or ""
                        return str(n).lower()

                    for m in markets:
                        if not isinstance(m, dict):
                            continue
                        n = mname(m)
                        if (moneyline is None) and ("1x2" in n or "moneyline" in n or "winner" in n):
                            moneyline = m
                        if (total_main is None) and ("total" in n or ("over" in n and "under" in n)):
                            total_main = m
                        if (handicap_main is None) and ("handicap" in n or "spread" in n):
                            handicap_main = m

                logger.info("SportAPI match_odds OK: requested=%s used_sport=%s used_path=%s", sport_slug, sport, used_path)
                return OddsSnapshot(
                    raw={"_used_path": used_path, **raw} if isinstance(raw, dict) else {"_used_path": used_path, "raw": raw},
                    moneyline=moneyline,
                    total_main=total_main,
                    handicap_main=handicap_main,
                )
            except Exception as e:
                last_err = e
                if self._looks_like_no_such_sport(e):
                    continue
                continue

        raise SportAPIError(f"match_odds failed for sport_slug={sport_slug} match_id={match_id}: {last_err}")
