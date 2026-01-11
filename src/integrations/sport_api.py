# src/integrations/sport_api.py
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

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


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


class SportAPIClient:
    def __init__(self, base_url: str, api_key: str, ...):
        self.base_url = base_url
        ...

    """
    Универсальный клиент под Sport Events API (по твоим данным: /v2/<sport>/...).

    ENV:
      SPORT_API_BASE            например: https://api.<provider>.com
      SPORT_API_KEY             ключ
      SPORT_API_KEY_HEADER      например: X-Api-Key или Authorization
      SPORT_API_KEY_PREFIX      например: Bearer  (если используешь Authorization: Bearer <key>)
      SPORT_API_TIMEOUT_S       по умолчанию 12
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE")
        self.key = _env("SPORT_API_KEY")
        self.key_header = _env("SPORT_API_KEY_HEADER", "X-Api-Key")
        self.key_prefix = _env("SPORT_API_KEY_PREFIX", "")
        self.timeout_s = float(_env("SPORT_API_TIMEOUT_S", "12") or 12)

        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        if not self.key:
            raise SportAPIError("SPORT_API_KEY is missing")

    def _headers(self) -> Dict[str, str]:
        v = self.key
        if self.key_prefix:
            v = f"{self.key_prefix} {self.key}".strip()
        return {
            "Accept": "application/json",
            self.key_header: v,
        }

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = _join_url(self.base, path)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                r = await client.get(url, headers=self._headers(), params=params)
            except Exception as e:
                raise SportAPIError(f"request failed: {type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            txt = (r.text or "")[:300]
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
        # самые частые варианты у провайдеров
        # 1) title/name
        t = self._pick(m, ["title", "name", "eventName", "matchName"], "")
        if t:
            return str(t)

        # 2) home/away
        home = self._pick(m, ["homeName", "home_team", "homeTeam", "home", "team1"], "")
        away = self._pick(m, ["awayName", "away_team", "awayTeam", "away", "team2"], "")
        if home and away:
            return f"{home} — {away}"

        # 3) competitors list
        comps = m.get("competitors") or m.get("participants") or m.get("teams")
        if isinstance(comps, list) and len(comps) >= 2:
            a = comps[0].get("name") if isinstance(comps[0], dict) else str(comps[0])
            b = comps[1].get("name") if isinstance(comps[1], dict) else str(comps[1])
            if a and b:
                return f"{a} — {b}"

        return "Матч"

    def _infer_league(self, m: Dict[str, Any]) -> str:
        # league/tournament/competition
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
        # startTime/start_time/utcStartTime/date
        st = self._pick(m, ["start_time", "startTime", "utcStartTime", "start", "date", "scheduled"], "")
        if isinstance(st, dict):
            return str(self._pick(st, ["utc", "iso", "dateTime"], ""))
        return str(st or "")

    # ---------- public API used by parsing.py ----------
    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchItem]:
        """
        Ожидаем путь по твоим данным:
          /v2/<sport>/
        И параметр даты (у разных провайдеров может называться по-разному).
        Мы пошлём сразу несколько вариантов через params — обычно один из них сработает.
        """
        sport_slug = (sport_slug or "").strip().lower()
        base_path = f"v2/{sport_slug}/"

        params = {
            # самые типовые варианты
            "date": day.isoformat(),
            "day": day.isoformat(),
            "from": day.isoformat(),
            "to": day.isoformat(),
        }

        data = await self._get(base_path, params=params)

        # некоторые провайдеры возвращают список сразу, некоторые заворачивают в {"data": [...]}
        items = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("data", "events", "matches", "results", "items"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break

        if not isinstance(items, list):
            # чтобы было легче дебажить в логах
            raise SportAPIError(f"unexpected response shape for matches_by_date: {type(data).__name__}")

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
                )
            )
        return out

    async def match_details(self, sport_slug: str, match_id: str) -> MatchItem:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id).strip()
        # универсально: /v2/<sport>/<id>
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

        # raw храним полностью
        raw: Dict[str, Any]
        if isinstance(data, dict):
            raw = data
        else:
            raw = {"data": data}

        # Нормализация (очень мягкая — чтобы parsing.py мог кормить LLM)
        # Мы попытаемся вынуть что-то похожее на:
        # - moneyline/1x2
        # - main total
        # - main handicap
        moneyline = None
        total_main = None
        handicap_main = None

        # common wrappers
        root = data
        if isinstance(data, dict):
            for k in ("data", "odds", "markets", "result"):
                if k in data:
                    root = data[k]
                    break

        # если markets списком
        markets = None
        if isinstance(root, list):
            markets = root
        elif isinstance(root, dict):
            for k in ("markets", "items", "data", "lines"):
                if isinstance(root.get(k), list):
                    markets = root[k]
                    break

        if isinstance(markets, list):
            # ищем по name/key/type
            def mname(x: Dict[str, Any]) -> str:
                n = x.get("name") or x.get("key") or x.get("type") or ""
                return str(n).lower()

            for m in markets:
                if not isinstance(m, dict):
                    continue
                n = mname(m)
                if (moneyline is None) and ("1x2" in n or "moneyline" in n or "winner" in n):
                    moneyline = m
                if (total_main is None) and ("total" in n or "over" in n and "under" in n):
                    total_main = m
                if (handicap_main is None) and ("handicap" in n or "spread" in n):
                    handicap_main = m

        # если вообще ничего не нашли — ничего страшного, raw остаётся
        snap = OddsSnapshot(
            raw={"_used_path": used_path, **raw} if isinstance(raw, dict) else {"_used_path": used_path, "raw": raw},
            moneyline=moneyline,
            total_main=total_main,
            handicap_main=handicap_main,
        )
        return snap
