from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Optional, Dict, List

import httpx


# ===== ENV =====
API_BASE = (os.getenv("SPORT_API_BASE") or "").strip().rstrip("/")
API_KEY = (os.getenv("SPORT_API_KEY") or "").strip()
TIMEOUT_S = float(os.getenv("SPORT_API_TIMEOUT_S") or "12")

# ВАЖНО:
# У разных провайдеров ключ может идти по-разному.
# По умолчанию: Authorization: <KEY>
# Если у тебя "x-api-key" — поставь SPORT_API_KEY_HEADER=x-api-key
API_KEY_HEADER = (os.getenv("SPORT_API_KEY_HEADER") or "authorization").strip().lower()
API_KEY_PREFIX = (os.getenv("SPORT_API_KEY_PREFIX") or "").strip()  # например "Bearer "


class SportAPIError(RuntimeError):
    pass


@dataclass
class ApiMatch:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str = ""
    start_time: str = ""  # ISO string if known


@dataclass
class ApiOddsSnapshot:
    # универсальный снапшот, который мы потом скормим LLM
    moneyline: Optional[dict] = None
    total_main: Optional[dict] = None
    handicap_main: Optional[dict] = None
    raw: Optional[dict] = None


class SportAPIClient:
    def __init__(self) -> None:
        if not API_BASE:
            raise SportAPIError("SPORT_API_BASE is not set")
        if not API_KEY:
            raise SportAPIError("SPORT_API_KEY is not set")
        self.base = API_BASE
        self.key = API_KEY
        self.timeout = httpx.Timeout(TIMEOUT_S)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        value = f"{API_KEY_PREFIX}{self.key}".strip()
        if API_KEY_HEADER in ("authorization", "auth"):
            h["Authorization"] = value
        else:
            # пример: x-api-key: <KEY>
            h[API_KEY_HEADER] = value
        return h

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            r = await client.get(url, params=params)
            if r.status_code >= 400:
                raise SportAPIError(f"HTTP {r.status_code}: {r.text[:400]}")
            return r.json()

    def _pick_list(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "results", "items", "matches", "events"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
        return []

    def _pick_dict(self, data: Any) -> dict:
        if isinstance(data, dict):
            return data
        return {}

    def _name(self, x: Any, fallback: str = "") -> str:
        if isinstance(x, dict):
            return str(x.get("name") or x.get("title") or x.get("shortName") or fallback).strip()
        return str(x or fallback).strip()

    def _iso(self, x: Any) -> str:
        # стараемся вернуть ISO если это datetime/str
        if isinstance(x, str):
            return x.strip()
        if isinstance(x, (int, float)):
            # иногда приходит timestamp
            try:
                return datetime.utcfromtimestamp(float(x)).isoformat() + "Z"
            except Exception:
                return ""
        return ""

    def _extract_match_common(self, it: dict, sport_slug: str) -> ApiMatch:
        mid = it.get("id") or it.get("matchId") or it.get("eventId")
        match_id = str(mid)

        # команды
        home = (
            self._name(it.get("homeTeam")) or
            self._name(it.get("home")) or
            self._name((it.get("competitors") or [{}])[0]) or
            self._name((it.get("teams") or [{}])[0]) or
            "Home"
        )
        away = (
            self._name(it.get("awayTeam")) or
            self._name(it.get("away")) or
            self._name((it.get("competitors") or [{}, {}])[1]) or
            self._name((it.get("teams") or [{}, {}])[1]) or
            "Away"
        )
        title = f"{home} — {away}"

        league = (
            self._name(it.get("tournament")) or
            self._name(it.get("league")) or
            self._name(it.get("competition")) or
            str(it.get("league") or it.get("competition") or "").strip()
        )

        status = str(
            it.get("status") or
            (it.get("state") or {}).get("status") or
            (it.get("matchStatus") or {}).get("type") or
            ""
        ).strip()

        start_time = self._iso(
            it.get("startTime") or it.get("start_time") or it.get("kickoff") or it.get("scheduledAt")
        )

        return ApiMatch(
            id=match_id,
            sport_slug=sport_slug,
            title=title,
            league=league,
            status=status,
            start_time=start_time,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> list[ApiMatch]:
        sport_slug = (sport_slug or "").strip().lower()

        # Документ/пример из твоих данных: /v2/<sport>/...
        # Предполагаем matches endpoint.
        data = await self._get(f"/v2/{sport_slug}/matches", params={"date": day.isoformat()})
        items = self._pick_list(data)

        out: list[ApiMatch] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("id") is None and it.get("matchId") is None and it.get("eventId") is None:
                continue
            out.append(self._extract_match_common(it, sport_slug))
        return out

    async def match_details(self, sport_slug: str, match_id: str) -> ApiMatch:
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id).strip()

        # Пытаемся несколько вариантов, потому что у провайдера мог быть другой маршрут
        candidates = [
            f"/v2/{sport_slug}/matches/{match_id}",
            f"/v2/{sport_slug}/match/{match_id}",
            f"/v2/{sport_slug}/events/{match_id}",
        ]

        last_err: Optional[Exception] = None
        for path in candidates:
            try:
                data = await self._get(path)
                d = self._pick_dict(data)
                # иногда match лежит в data
                if "data" in d and isinstance(d["data"], dict):
                    d = d["data"]
                if not isinstance(d, dict) or not d:
                    continue
                return self._extract_match_common(d, sport_slug)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_details failed: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> ApiOddsSnapshot:
        """
        Возвращает универсальный снапшот:
        - moneyline: home/draw/away
        - total_main: value/over/under
        - handicap_main: team/value/odds
        Если провайдер отдаёт другое — мы сохраняем в raw и вытащим лучшее что можем.
        """
        sport_slug = (sport_slug or "").strip().lower()
        match_id = str(match_id).strip()

        candidates = [
            f"/v2/{sport_slug}/matches/{match_id}/odds",
            f"/v2/{sport_slug}/matches/{match_id}/markets",
            f"/v2/{sport_slug}/odds/{match_id}",
            f"/v2/{sport_slug}/markets/{match_id}",
        ]

        data: Any = None
        last_err: Optional[Exception] = None
        for path in candidates:
            try:
                data = await self._get(path)
                break
            except Exception as e:
                last_err = e

        if data is None:
            raise SportAPIError(f"match_odds failed: {last_err}")

        raw = data if isinstance(data, dict) else {"data": data}

        # Ищем рынки в data/results/items/markets
        root = raw
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            root = raw["data"]

        markets = []
        if isinstance(root, dict):
            for k in ("markets", "items", "results", "data"):
                v = root.get(k)
                if isinstance(v, list):
                    markets = v
                    break
        if not markets and isinstance(raw, dict):
            for k in ("markets", "items", "results", "data"):
                v = raw.get(k)
                if isinstance(v, list):
                    markets = v
                    break

        moneyline = None
        total_main = None
        handicap_main = None

        def fnum(x: Any) -> Optional[float]:
            try:
                if x is None:
                    return None
                return float(str(x).replace(",", "."))
            except Exception:
                return None

        # максимально терпимый парсер
        for m in markets:
            if not isinstance(m, dict):
                continue
            key = str(m.get("key") or m.get("type") or m.get("name") or "").lower()

            outcomes = m.get("outcomes") or m.get("selections") or m.get("runners") or []
            if not isinstance(outcomes, list):
                outcomes = []

            if moneyline is None and any(x in key for x in ("1x2", "moneyline", "match_result", "result")):
                home = draw = away = None
                for o in outcomes:
                    if not isinstance(o, dict):
                        continue
                    on = str(o.get("name") or o.get("type") or "").lower()
                    price = fnum(o.get("odds") or o.get("price") or o.get("value"))
                    if price is None:
                        continue
                    if on in ("1", "home", "team1"):
                        home = price
                    elif on in ("x", "draw", "tie"):
                        draw = price
                    elif on in ("2", "away", "team2"):
                        away = price
                if home or draw or away:
                    moneyline = {"home": home, "draw": draw, "away": away}

            if total_main is None and ("total" in key or "over_under" in key or "ou" == key):
                # value может быть у маркета или у outcomes
                line = fnum(m.get("line") or m.get("value") or m.get("total"))
                over = under = None
                for o in outcomes:
                    if not isinstance(o, dict):
                        continue
                    on = str(o.get("name") or o.get("type") or "").lower()
                    price = fnum(o.get("odds") or o.get("price") or o.get("value"))
                    if price is None:
                        continue
                    if "over" in on or "больше" in on:
                        over = price
                        if line is None:
                            line = fnum(o.get("line") or o.get("handicap"))
                    if "under" in on or "меньше" in on:
                        under = price
                        if line is None:
                            line = fnum(o.get("line") or o.get("handicap"))
                if line is not None or over is not None or under is not None:
                    total_main = {"value": line, "over": over, "under": under}

            if handicap_main is None and ("handicap" in key or "spread" in key or "фора" in key):
                # часто в outcomes handicap/line
                team = None
                line = fnum(m.get("line") or m.get("handicap") or m.get("value"))
                odds = None
                for o in outcomes:
                    if not isinstance(o, dict):
                        continue
                    price = fnum(o.get("odds") or o.get("price") or o.get("value"))
                    h = fnum(o.get("handicap") or o.get("line") or o.get("value"))
                    on = str(o.get("name") or o.get("type") or "").lower()
                    if line is None and h is not None:
                        line = h
                    if odds is None and price is not None:
                        odds = price
                    if team is None and on:
                        if "home" in on or "team1" in on or on == "1":
                            team = "home"
                        elif "away" in on or "team2" in on or on == "2":
                            team = "away"
                if line is not None or odds is not None:
                    handicap_main = {"team": team or "home", "value": line, "odds": odds}

        return ApiOddsSnapshot(
            moneyline=moneyline,
            total_main=total_main,
            handicap_main=handicap_main,
            raw=raw if isinstance(raw, dict) else {"data": raw},
        )
