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
    country: str
    status: str
    start_time: str  # как приходит от провайдера (обычно ISO)
    score: str = ""  # текущий/итоговый счёт
    odds_base: Optional[Dict[str, Any]] = None  # базовые коэффициенты/рынки если есть


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


class SportAPIClient:
    """
    Клиент под api.api-sport.ru (и похожие провайдеры).

    ENV:
      SPORT_API_BASE            например: https://api.api-sport.ru
      SPORT_API_KEY             ключ
      SPORT_API_KEY_HEADER      например: Authorization
      SPORT_API_KEY_PREFIX      например: Bearer
      SPORT_API_TIMEOUT_S       по умолчанию 12
      SPORT_API_BASE_PATH       необязательно
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

    def _headers(self) -> Dict[str, str]:
        v = self.key
        if self.key_prefix:
            v = f"{self.key_prefix} {self.key}".strip()
        return {"Accept": "application/json", self.key_header: v}

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

    # ---------- helpers ----------
    def _pick(self, obj: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        return default

    def _stringify_id(self, x: Any) -> str:
        if x is None:
            return ""
        return str(x).strip()

    def _pick_translation_ru(self, obj: Any) -> str:
        """Если obj dict и есть translations.ru — возвращаем."""
        if isinstance(obj, dict):
            tr = obj.get("translations")
            if isinstance(tr, dict):
                ru = tr.get("ru")
                if isinstance(ru, str) and ru.strip():
                    return ru.strip()
        return ""

    def _team_name(self, v: Any) -> str:
        """Команда может быть строкой или dict. Берём translations.ru -> name -> str."""
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()

        if isinstance(v, dict):
            ru = self._pick_translation_ru(v)
            if ru:
                return ru

            name = v.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

        return str(v).strip()

    def _league_name(self, v: Any) -> str:
        """Лига может быть строкой или dict. translations.ru -> name/title."""
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()

        if isinstance(v, dict):
            ru = self._pick_translation_ru(v)
            if ru:
                return ru

            name = v.get("name") or v.get("title")
            if isinstance(name, str) and name.strip():
                return name.strip()

        return str(v).strip()

    def _country_name(self, v: Any) -> str:
        """Страна может быть строкой или dict."""
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            ru = self._pick_translation_ru(v)
            if ru:
                return ru
            name = v.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return str(v).strip()

    def _score_num(self, v: Any) -> Optional[int]:
        """Вытащить число (current/total/final) из разных форматов."""
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            try:
                return int(v)
            except Exception:
                return None
        if isinstance(v, str):
            s = v.strip()
            if s.isdigit():
                return int(s)
            return None
        if isinstance(v, dict):
            # приоритет: final/total -> current
            for k in ("final", "total", "full", "ft", "result", "current"):
                if k in v:
                    x = v.get(k)
                    if isinstance(x, (int, float)):
                        return int(x)
                    if isinstance(x, str) and x.strip().isdigit():
                        return int(x.strip())
            # иногда просто {"home":0,"away":0}
            if "home" in v and isinstance(v.get("home"), (int, float)):
                return int(v.get("home"))
        return None

    def _infer_home_away(self, m: Dict[str, Any]) -> Tuple[Any, Any]:
        """
        Под разные ключи api-sport:
        home/away, homeTeam/awayTeam, teams: {home:{}, away:{}}, competitors[0/1]
        """
        home = self._pick(m, ["home", "homeTeam", "home_team", "team1", "homeName"], None)
        away = self._pick(m, ["away", "awayTeam", "away_team", "team2", "awayName"], None)

        if home or away:
            return home, away

        teams = m.get("teams")
        if isinstance(teams, dict):
            h = teams.get("home")
            a = teams.get("away")
            if h or a:
                return h, a

        comps = m.get("competitors") or m.get("participants") or m.get("teams")
        if isinstance(comps, list) and len(comps) >= 2:
            return comps[0], comps[1]

        return None, None

    def _infer_title(self, m: Dict[str, Any]) -> str:
        # 1) title/name если это строка
        t = self._pick(m, ["title", "name", "eventName", "matchName"], "")
        if isinstance(t, str) and t.strip():
            return t.strip()

        # 2) home/away
        home_raw, away_raw = self._infer_home_away(m)
        home = self._team_name(home_raw)
        away = self._team_name(away_raw)
        if home and away:
            return f"{home} — {away}"

        return "Матч"

    def _infer_league(self, m: Dict[str, Any]) -> str:
        league = self._pick(m, ["league", "tournament", "competition", "leagueName", "competitionName"], "")
        return self._league_name(league) or "Other"

    def _infer_country(self, m: Dict[str, Any]) -> str:
        # прямой ключ
        c = m.get("country")
        cn = self._country_name(c)
        if cn:
            return cn

        # иногда страна лежит внутри league/tournament/competition
        league = self._pick(m, ["league", "tournament", "competition"], None)
        if isinstance(league, dict):
            cn2 = self._country_name(league.get("country"))
            if cn2:
                return cn2

        # иногда team.home.country
        home_raw, _ = self._infer_home_away(m)
        if isinstance(home_raw, dict):
            cn3 = self._country_name(home_raw.get("country"))
            if cn3:
                return cn3

        return "Other"

    def _infer_status(self, m: Dict[str, Any]) -> str:
        s = self._pick(m, ["status", "state", "matchStatus", "eventStatus"], "")
        if isinstance(s, dict):
            v = self._pick(s, ["type", "code", "name"], "")
            return str(v).strip()
        return str(s or "").strip()

    def _infer_start_time(self, m: Dict[str, Any]) -> str:
        st = self._pick(m, ["start_time", "startTime", "utcStartTime", "start", "date", "scheduled"], "")
        if isinstance(st, dict):
            v = self._pick(st, ["utc", "iso", "dateTime"], "")
            return str(v).strip()
        return str(st or "").strip()

    def _infer_score(self, m: Dict[str, Any]) -> str:
        """
        Нормализуем score под частые форматы api.api-sport.ru.
        Хотим:
        - LIVE: текущий счёт (current)
        - FINISHED: итоговый (final/total)
        """
        status = (self._infer_status(m) or "").lower()

        s = m.get("score") or m.get("scores") or m.get("result") or m.get("liveScore")
        if isinstance(s, str):
            return s.strip()

        # api-sport часто: score: { home: {current:0, total:2}, away: {current:0, total:1} }
        if isinstance(s, dict):
            home = s.get("home") if s.get("home") is not None else s.get("homeScore")
            away = s.get("away") if s.get("away") is not None else s.get("awayScore")

            if status in {"finished", "ended"}:
                # приоритет total/final
                h = self._score_num(home if isinstance(home, dict) else {"total": home})
                a = self._score_num(away if isinstance(away, dict) else {"total": away})
                if h is not None and a is not None:
                    return f"{h}:{a}"

            # иначе current
            if isinstance(home, dict) and isinstance(away, dict):
                hc = self._score_num(home)
                ac = self._score_num(away)
                if hc is not None and ac is not None:
                    return f"{hc}:{ac}"

            # формат: {"current":{"home":0,"away":0}}
            cur = s.get("current")
            if isinstance(cur, dict):
                hh = self._score_num(cur.get("home"))
                aa = self._score_num(cur.get("away"))
                if hh is not None and aa is not None:
                    return f"{hh}:{aa}"

        # fallback: отдельные поля
        home = m.get("homeScore")
        away = m.get("awayScore")
        if isinstance(home, (int, float)) and isinstance(away, (int, float)):
            return f"{int(home)}:{int(away)}"

        return ""

    def _infer_odds_base(self, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidates = ["oddsBase", "odds_base", "odds", "line", "prematchOdds", "prematch", "bookmakers", "markets"]
        for k in candidates:
            v = m.get(k)
            if isinstance(v, dict):
                return v
            if isinstance(v, list):
                return {"items": v, "_source_key": k}
        return None

    # ---------- public API used by parsing.py ----------
    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchItem]:
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
            f"v2/{sport_slug}/matches",
            f"v2/{sport_slug}/events",
            f"v2/{sport_slug}/",
            f"v2/{sport_slug}",
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
                    country=self._infer_country(m),
                    status=self._infer_status(m),
                    start_time=self._infer_start_time(m),
                    score=self._infer_score(m),
                    odds_base=self._infer_odds_base(m),
                )
            )

        return out

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
    """
    FIX: api.api-sport.ru НЕ поддерживает /v2/<sport>/<id>
    Нормальный details: /v2/<sport>/matches/<id>
    """
    sport_slug = sport_slug.strip()
    match_id = str(match_id).strip()

    candidates = [
        f"/v2/{sport_slug}/matches/{match_id}",
        f"/v2/{sport_slug}/events/{match_id}",
        # если у провайдера есть query-style:
        (f"/v2/{sport_slug}/matches", {"id": match_id}),
    ]

    last_err = None

    for item in candidates:
        try:
            if isinstance(item, tuple):
                path, params = item
                data = await self._get(path, params=params)  # <- у тебя в файле судя по логам _get()
            else:
                path = item
                data = await self._get(path)

            obj = self._unwrap_obj(data)
            if obj:
                return self._match_to_dto(obj, sport_slug)

        except Exception as e:
            last_err = e

    raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

