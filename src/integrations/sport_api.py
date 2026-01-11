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
    score: str = ""  # текущий счёт/результат если есть (строкой)
    odds_base: Optional[Dict[str, Any]] = None  # базовые коэффициенты/рынки если есть в списке матчей


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


class SportAPIClient:
    """
    Универсальный клиент под Sport Events API.

    ENV:
      SPORT_API_BASE            например: https://api.api-sport.ru
      SPORT_API_KEY             ключ
      SPORT_API_KEY_HEADER      например: X-Api-Key или Authorization
      SPORT_API_KEY_PREFIX      например: Bearer  (если используешь Authorization: Bearer <key>)
      SPORT_API_TIMEOUT_S       по умолчанию 12
      SPORT_API_BASE_PATH       необязательно: если у провайдера все методы лежат под префиксом
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
            "SportAPI init: base=%r scheme=%r host=%r header=%r prefix=%r timeout=%s key_present=%s",
            self.base,
            u.scheme,
            u.netloc,
            self.key_header,
            self.key_prefix,
            self.timeout_s,
            bool(self.key),
        )

    def _headers(self(self) -> Dict[str, str]:
        v = self.key
        if self.key_prefix:
            v = f"{self.key_prefix} {self.key}".strip()
        return {
            "Accept": "application/json",
            self.key_header: v,
        }

    # backward compatible name (typo-safe)
    def _headers(self) -> Dict[str, str]:
        return self._headers_R()

    def _normalize_path(self, path: str) -> str:
        path = (path or "").lstrip("/")
        if self.base_path:
            return f"{self.base_path}/{path}".lstrip("/")
        return path

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        path = self._normalize_path(path)
        url = _join_url(self.base, path)

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                r = await client.get(url, headers=self._headers(), params=params)
            except Exception as e:
                raise SportAPIError(f"request failed: {type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            txt = (r.text or "")[:600]
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
        if x is None:
            return ""
        return str(x)

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

    def _infer_score(self, m: Dict[str, Any]) -> str:
        s = m.get("score") or m.get("scores") or m.get("result") or m.get("liveScore")
        if isinstance(s, str):
            return s.strip()
        if isinstance(s, (int, float)):
            return str(s)

        if isinstance(s, dict):
            home = s.get("home") if s.get("home") is not None else s.get("homeScore")
            away = s.get("away") if s.get("away") is not None else s.get("awayScore")
            if home is not None and away is not None:
                return f"{home}:{away}"

            cur = s.get("current")
            if isinstance(cur, dict):
                h = cur.get("home")
                a = cur.get("away")
                if h is not None and a is not None:
                    return f"{h}:{a}"

        home = m.get("homeScore")
        away = m.get("awayScore")
        if home is not None and away is not None:
            return f"{home}:{away}"

        home_obj = m.get("home")
        away_obj = m.get("away")
        if isinstance(home_obj, dict) and isinstance(away_obj, dict):
            h = home_obj.get("score")
            a = away_obj.get("score")
            if h is not None and a is not None:
                return f"{h}:{a}"

        return ""

    def _infer_odds_base(self, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        В списке матчей некоторые провайдеры уже отдают базовые коэффициенты/рынки.
        parsing.py ожидает поле odds_base — поэтому возвращаем dict или None.
        """
        candidates = [
            "odds_base",
            "oddsBase",
            "odds",
            "markets",
            "line",
            "prematchOdds",
            "prematch",
            "bookmakers",
        ]
        for k in candidates:
            v = m.get(k)
            if isinstance(v, dict):
                return v
            if isinstance(v, list):
                # иногда markets/odds списком — завернём в dict чтобы тип был стабильный
                return {"items": v, "_source_key": k}
        return None

    # ---------- public API used by parsing.py ----------
    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchItem]:
        """
        Для api.api-sport.ru у тебя сработало:
          GET /v2/<sport>/matches?...

        Поэтому делаем несколько кандидатов путей и берём первый успешный.
        """
        sport_slug = (sport_slug or "").strip().lower()
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

        candidates = [
            f"v2/{sport_slug}/",
            f"v2/{sport_slug}",
            f"v2/{sport_slug}/events",
            f"v2/{sport_slug}/matches",
            f"v2/{sport_slug}/games",
            f"v2/{sport_slug}/list",
        ]

        last_err: Optional[Exception] = None
        data: Any = None
        used_path = ""

        for p in candidates:
            try:
                logger.info("SportAPI try matches_by_date sport=%s: GET %s params=%s", sport_slug, p, params)
                data = await self._get(p, params=params)
                used_path = p
                break
            except Exception as e:
                last_err = e
                logger.warning("SportAPI failed matches_by_date sport=%s on path=%s err=%s", sport_slug, p, e)

        if data is None:
            raise SportAPIError(
                f"matches_by_date failed for sport_slug={sport_slug}: "
                f"all endpoints failed for matches_by_date sport={sport_slug}: {last_err}"
            )

        items = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("data", "events", "matches", "results", "items"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break

        if not isinstance(items, list):
            raise SportAPIError(f"unexpected response shape for matches_by_date: {type(data).__name__}")

        logger.info(
            "SportAPI matches_by_date OK: requested=%s used_sport=%s used_path=%s n=%s",
            sport_slug,
            sport_slug,
            used_path,
            len(items),
        )

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
                    sport_slug=sport_slug,
                    title=self._infer_title(m),
                    league=self._infer_league(m),
                    status=self._infer_status(m),
                    start_time=self._infer_start_time(m),
                    score=self._infer_score(m),
                    odds_base=self._infer_odds_base(m),
                )
            )
        return out

    async def match_details(self, sport_slug: str, match_id: str) -> MatchItem:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id).strip()
        path = f"v2/{sport_slug}/{match_id}"
        data = await self._get(path)

        if isinstance(data, dict):
            m = data.get("data") if isinstance(data.get("data"), dict) else data
            if not isinstance(m, dict):
                m = data
        else:
            raise SportAPIError(f"unexpected response shape for match_details: {type(data).__name__}")

        return MatchItem(
            id=match_id,
            sport_slug=sport_slug,
            title=self._infer_title(m),
            league=self._infer_league(m),
            status=self._infer_status(m),
            start_time=self._infer_start_time(m),
            score=self._infer_score(m),
            odds_base=self._infer_odds_base(m),
        )

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        """
        У разных провайдеров odds лежат по-разному. Делаем максимально мягко:
        пробуем /odds, /markets, /line — и берём, что вернулось.
        """
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id).strip()

        candidates = [
            f"v2/{sport_slug}/{match_id}/odds",
            f"v2/{sport_slug}/{match_id}/markets",
            f"v2/{sport_slug}/{match_id}/line",
        ]

        last_err: Optional[Exception] = None
        data: Any = None
        used_path = ""

        for p in candidates:
            try:
                data = await self._get(p)
                used_path = p
                break
            except Exception as e:
                last_err = e

        if data is None:
            raise SportAPIError(f"all odds endpoints failed: {last_err}")

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

        snap = OddsSnapshot(
            raw={"_used_path": used_path, **raw} if isinstance(raw, dict) else {"_used_path": used_path, "raw": raw},
            moneyline=moneyline,
            total_main=total_main,
            handicap_main=handicap_main,
        )
        return snap
