# src/parsing.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional, Tuple, Dict, Any, List

from sqlmodel import Session, select
from zoneinfo import ZoneInfo
from sqlalchemy import text

from .db import get_session
from . import bets_db
from .llm_client import analyze_with_llm_cached
from .pro_db import is_pro

logger = logging.getLogger(__name__)

# Admin guard: only this user can run debug/admin commands
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# ============================================================
# Telegram limits
# ============================================================
# Telegram API hard limit is 4096 UTF-8 chars for message text.
# Keep some headroom for markdown/markup and service text.
TELEGRAM_MAX_CHARS: int = int(os.getenv('TELEGRAM_MAX_CHARS', '3900'))

# LLM cache TTLs (seconds). Keep short for LIVE, longer for PRE.
TTL_PRE_S: int = int(os.getenv('TTL_PRE_S', '600'))
TTL_LIVE_S: int = int(os.getenv('TTL_LIVE_S', '45'))
TTL_LIVE_PRO_S: int = int(os.getenv('TTL_LIVE_PRO_S', '30'))


# --- LLM cooldown (anti-spam on 429 / insufficient_quota) ---
_LLM_DISABLED_UNTIL_TS = 0
_LLM_DISABLED_REASON = ""
# ============================================================
# In-memory per-user caches (no DB required)
# ============================================================
# Used to remember last selected sport and last match snapshot for UI actions.
# Structure: {user_id: {match_id: match_meta_dict}}
_MATCH_CACHE_BY_USER: Dict[int, Dict[str, Dict[str, Any]]] = {}
# Structure: {user_id: sport_slug}
_ACTIVE_SPORT_BY_USER: Dict[int, str] = {}

# Stores last LLM meta for UI debug / consistency (per user).
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}

# Per-user navigation state (used by text-based nav in run_dialog_agent)
_ACTIVE_COUNTRY_BY_USER: Dict[int, str] = {}
_ACTIVE_LEAGUE_BY_USER: Dict[int, str] = {}
_ACTIVE_PAGE_BY_USER: Dict[int, int] = {}

# Stores last LIVE snapshot per match_id to detect changes (used in LIVE mode).
# Structure: {match_id: {snapshot fields...}}
_LIVE_SNAPSHOT_BY_MATCH: Dict[str, Dict[str, Any]] = {}

# Cache: last rendered LIVE text per (match_id, action).
# Used to avoid re-sending identical text when provider data didn't change.
_LIVE_RENDER_BY_MATCH: Dict[tuple, str] = {}

# LIVE data cache: {f"{sport}:{match_id}" → (timestamp, extras_data)}
_LIVE_DATA_CACHE: Dict[str, tuple] = {}
_LIVE_DATA_CACHE_TTL = 60  # seconds

# User refresh cooldown: {user_id → last_refresh_timestamp}
_USER_REFRESH_COOLDOWN: Dict[int, float] = {}
_REFRESH_COOLDOWN_S = 30  # seconds

# LIVE status strings (uppercase normalized)
_LIVE_STATUSES = {
    # Hockey
    "P1", "P2", "P3", "OT", "BT",
    # Football
    "1H", "2H", "HT", "ET", "P",
    # Basketball
    "Q1", "Q2", "Q3", "Q4",
    # MMA
    "IN_PROGRESS", "R1", "R2", "R3", "R4", "R5",
    # Tennis
    "S1", "S2", "S3", "S4", "S5",
    # Generic
    "INPROGRESS", "LIVE",
}


def _is_live_match(status: str) -> bool:
    """Detect if match is currently in progress."""
    s = (status or "").strip().upper()
    return s in _LIVE_STATUSES or "live" in s.lower() or "inprogress" in s.lower().replace("_", "")


async def _fetch_live_extras(sport: str, match_id: str) -> Dict[str, Any]:
    """Fetch LIVE statistics/events with 60s cache."""
    key = f"{sport}:{match_id}"
    now = time.time()

    cached = _LIVE_DATA_CACHE.get(key)
    if cached:
        ts, data = cached
        if now - ts < _LIVE_DATA_CACHE_TTL:
            return data

    try:
        from .integrations.sport_api import SportAPIClient
        api = SportAPIClient()
        extras = await api.match_extras(sport, match_id)
        _LIVE_DATA_CACHE[key] = (now, extras)
        logger.info("LIVE extras fetched: sport=%s match=%s sources=%d",
                     sport, match_id, len((extras or {}).get("sources", {})))
        return extras
    except Exception:
        logger.exception("_fetch_live_extras failed sport=%s match=%s", sport, match_id)
        return {}


def _merge_extras_into_raw(raw: Dict[str, Any], extras: Dict[str, Any]) -> Dict[str, Any]:
    """Merge match_extras() results into raw dict for feature extraction."""
    sources = extras.get("sources") or {}
    stats_merged = False
    events_merged = False

    for path, data in sources.items():
        if not data:
            continue

        # Statistics endpoint responses
        if not stats_merged and ("statistics" in path or "stats" in path):
            stats = None
            if isinstance(data, dict):
                # Unwrap: {data: [...]} or {response: [...]} or {statistics: [...]}
                for k in ("data", "response", "statistics", "stats", "results"):
                    v = data.get(k)
                    if isinstance(v, (dict, list)):
                        stats = v
                        break
                if stats is None and any(k in data for k in ("shotsOnGoal", "shots", "penalties")):
                    stats = data  # direct stats object
            elif isinstance(data, list):
                stats = data

            if stats is not None:
                raw["statistics"] = stats
                stats_merged = True
                logger.info("LIVE stats merged from %s type=%s", path, type(stats).__name__)

        # Events/incidents endpoint responses
        if not events_merged and ("events" in path or "incidents" in path):
            events = None
            if isinstance(data, dict):
                for k in ("data", "response", "events", "incidents", "results"):
                    v = data.get(k)
                    if isinstance(v, list):
                        events = v
                        break
            elif isinstance(data, list):
                events = data

            if events is not None:
                raw["events"] = events
                events_merged = True
                logger.info("LIVE events merged from %s count=%d", path, len(events))

    return raw

# Human-friendly sport labels used in UI texts (expand freely).
API_SPORTS_LABELS: Dict[str, str] = {
    'ice-hockey': '🏒 Хоккей',
    'football': '⚽ Футбол',
    'basketball': '🏀 Баскетбол',
    'tennis': '🎾 Теннис',
    'volleyball': '🏐 Волейбол',
    'handball': '🤾 Гандбол',
    'baseball': '⚾ Бейсбол',
    'mma': '🥊 MMA',
    'boxing': '🥊 Бокс',
    'esports': '🎮 Киберспорт',
}





def _now_ts() -> int:
    return int(time.time())


def _is_quota_error(err: Exception) -> bool:
    s = str(err)
    return (
        "insufficient_quota" in s
        or "You exceeded your current quota" in s
        or "HTTP 429" in s
        or '"code": "insufficient_quota"' in s
    )


def _llm_is_disabled() -> bool:
    return _now_ts() < int(_LLM_DISABLED_UNTIL_TS or 0)


def _llm_trip_disable(minutes: int = 20, reason: str = "quota/429") -> None:
    global _LLM_DISABLED_UNTIL_TS, _LLM_DISABLED_REASON
    _LLM_DISABLED_REASON = str(reason or "quota/429")[:200]
    _LLM_DISABLED_UNTIL_TS = _now_ts() + int(minutes) * 60


def _fallback_analysis(
    reason: str,
    *,
    mode: str | None = None,
    snapshot: dict | None = None,
) -> dict:
    """Fallback analysis dict (used when LLM is unavailable).

    Важно: не пишем пользователю «AI недоступен». Возвращаем полезную «быструю аналитику»
    на основе доступных данных (команды/счёт/статус/лига), чтобы бот выглядел стабильно.
    """
    mode = (mode or "").strip().lower()
    snapshot = snapshot or {}

    def _g(key: str, default: str = "") -> str:
        v = snapshot.get(key)
        return str(v).strip() if v is not None else default

    home = _g("home") or _g("team_a") or "Хозяева"
    away = _g("away") or _g("team_b") or "Гости"
    league = _g("league")
    country = _g("country")
    date = _g("date")
    status = _g("status")
    score = _g("score")

    title_prefix = "📌 Быстрый разбор"
    if mode.startswith("pre"):
        title_prefix = "📌 Быстрый PRE-разбор"
    elif mode.startswith("live"):
        title_prefix = "🟢 Быстрый LIVE-разбор"
    elif "daily" in mode:
        title_prefix = "🗓 Быстрый обзор дня"

    title = f"{title_prefix}: {home} — {away}".strip()

    meta_bits = [b for b in [league, country] if b]
    meta = " • ".join(meta_bits)

    lines = []
    if meta:
        lines.append(meta)
    if date:
        lines.append(f"🕒 {date}")
    if score:
        lines.append(f"Счёт: {score}")
    if status and not score:
        lines.append(f"Статус: {status}")

    # Базовый анализ на основе доступных данных матча
    if mode.startswith("pre"):
        if score:
            lines += ["", "Матч уже начался. Для актуального разбора нажмите LIVE."]
        else:
            # Добавляем конкретику только если есть данные
            lines.append("")
            lines.append("Данные для анализа временно недоступны.")
            lines.append("Попробуйте обновить через 30 секунд.")
    else:
        lines += [
            "",
            "На что смотреть в LIVE:",
            "• темп — кто больше атакует последние 5 минут",
            "• движение линии — если кэф на победу резко упал, рынок получил информацию",
        ]

    return {
        "title": title,
        "summary": "\n".join(lines).strip(),
        "recommendation": "",
        "confidence": 0,
        "key_facts": [],
        "risks": [
            "Минимум данных — воздержитесь от ставки до появления актуальных данных.",
        ],
        "disclaimer": "Аналитический материал, не является рекомендацией.",
        "_fallback": True,
        "_reason": reason,
    }

async def analyze_with_llm_cached_safe(*args, **kwargs):
    """Safe wrapper around analyze_with_llm_cached().

    - Accepts extra UI kwargs (mode/action/snapshot/etc.) and strips them before calling.
    - Any exception falls back to a deterministic Russian 'умный' ответ, so bot doesn't выглядеть сломанным.
    """
    # UI / meta (may be passed by higher-level handlers)
    mode = kwargs.pop("mode", None)
    action = kwargs.pop("action", None)

    # Snapshot variants (older/newer call sites)
    snapshot = kwargs.pop("snapshot", None)
    prev_snapshot = kwargs.pop("prev_snapshot", None)
    cur_snapshot = kwargs.pop("cur_snapshot", None)
    prev_snap = kwargs.pop("prev_snap", None)
    cur_snap = kwargs.pop("cur_snap", None)

    match_meta = kwargs.pop("match_meta", None)
    match_id = kwargs.pop("match_id", None)

    # Prefer the most informative snapshot for fallback
    if cur_snap is None:
        cur_snap = cur_snapshot
    if cur_snap is None:
        cur_snap = snapshot
    if prev_snap is None:
        prev_snap = prev_snapshot

    try:
        # Important: analyze_with_llm_cached MUST NOT receive UI-only kwargs
        return await analyze_with_llm_cached(*args, **kwargs)
    except Exception as e:
        # Quota / rate-limit / transient errors should not break UX
        reason = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("LLM failed (safe wrapper), using fallback: %s", reason)
        try:
            text = _fallback_analysis(reason, mode=mode, snapshot=cur_snap)
        except Exception:
            # last-resort
            text = "• AI временно недоступен — показываю базовую справку.\n\nРиски\n• Недостаточно данных для детального разбора.\n\nАналитический материал, не является рекомендацией."
        meta = {
            "ok": False,
            "reason": reason,
            "mode": mode,
            "action": action,
            "match_id": match_id,
            "has_snapshot": bool(cur_snap),
        }
        return text, meta

def _msk_today_date():
    """
    Safe Moscow date resolver.
    Never raises NameError or timezone errors.
    """
    try:
        return datetime.now(ZoneInfo("Europe/Moscow")).date()
    except Exception:
        return datetime.utcnow().date()



def _md_escape(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("[", "\\[")
        .replace("`", "\\`")
    )


def _truncate_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 20)] + "\n…(обрезано)"


@contextmanager
def db_session() -> Session:
    gen = get_session()
    session = next(gen)
    try:
        yield session
    finally:
        try:
            gen.close()
        except Exception:
            pass


# -----------------------------
# TRIAL LIVE PRO (1 раз на пользователя)
# -----------------------------
def _trial_live_used(session: Session, user_id: int) -> bool:
    try:
        row = session.exec(
            text(
                """
                SELECT trial_live_used
                FROM users
                WHERE tg_user_id = :uid OR id = :uid
                LIMIT 1
                """
            ),
            params={"uid": int(user_id)},
        ).first()
        if not row:
            return False
        try:
            m = row._mapping  # type: ignore[attr-defined]
            return bool(m.get("trial_live_used"))
        except Exception:
            try:
                return bool(row[0])
            except Exception:
                return False
    except Exception:
        logger.exception("_trial_live_used failed")
        return False


def _consume_trial_live(session: Session, user_id: int) -> bool:
    try:
        session.exec(
            text(
                """
                INSERT INTO users (id, tg_user_id, trial_live_used, created_at, updated_at)
                VALUES (:uid, :uid, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    tg_user_id = EXCLUDED.tg_user_id,
                    trial_live_used = TRUE,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            params={"uid": int(user_id)},
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("_consume_trial_live failed")
        return False


# -----------------------------
# Группировка матчей: страна -> лига -> матчи
# -----------------------------
def _norm_key(s: Any) -> str:
    """
    Нормализуем ключи группировки (страна/лига).
    ВАЖНО: 'Other' больше не используем вообще.
    Всё пустое/None/'other' уходит в International.
    """
    v = (str(s or "")).strip()
    if not v:
        return "International"
    low = v.lower()
    if low in {"other", "unknown", "none", "null", "n/a", "-"}:
        return "International"
    return v



def _build_index_for_user(user_id: int) -> Dict[str, Any]:
    """
    Возвращаем:
      {
        "countries": { country: { league: [match_id] } },
        "match_meta": { match_id: meta }
      }
    """
    cache = _MATCH_CACHE_BY_USER.get(user_id) or {}
    countries: Dict[str, Dict[str, List[str]]] = {}

    for mid, meta in cache.items():
        country = _norm_key(meta.get("country") or meta.get("league_country") or "Other")
        league = _norm_key(meta.get("league") or "Other")
        countries.setdefault(country, {}).setdefault(league, []).append(mid)

    for c, leagues in countries.items():
        for lg, ids in leagues.items():
            ids.sort(key=lambda _id: str((cache.get(_id) or {}).get("start_time") or ""))

    return {"countries": countries, "match_meta": cache}


def _render_countries(user_id: int, sport_title: str, today_iso: str) -> str:
    idx = _build_index_for_user(user_id)

    # ❌ убираем Other полностью
    if "Other" in idx:
        # пробуем перераспределить в International
        idx.setdefault("International", {})
        for league, ids in idx["Other"].items():
            idx["International"].setdefault(league, []).extend(ids)
        del idx["Other"]

    items: list[tuple[str, int]] = []
    for country, leagues in idx.items():
        n = sum(len(v) for v in leagues.values())
        if n > 0:
            items.append((country, n))

    items.sort(key=lambda x: x[1], reverse=True)

    lines = [
        f"🏟 Матчи сегодня (по МСК) — {sport_title}",
        f"Дата: {today_iso}",
        "",
        "Выбери страну:",
    ]

    for c, n in items:
        lines.append(f"• {c} ({n})")

    if not items:
        lines.append("• Матчи не найдены")

    lines.append("")
    lines.append("Команды навигации:")
    lines.append("• страна: <название>")
    lines.append("• лига: <страна> | <лига> | <страница?>")

    return _truncate_telegram("\n".join(lines))



def _render_leagues(user_id: int, country: str) -> str:
    idx = _build_index_for_user(user_id)["countries"]
    leagues = idx.get(country) or {}
    items = [(lg, len(ids)) for lg, ids in leagues.items()]
    items.sort(key=lambda x: x[1], reverse=True)

    lines = [f"🏳️ Страна: {country}", "", "Выбери лигу:"]
    for lg, n in items[:15]:
        lines.append(f"• {lg} ({n})")
    if not items:
        lines.append("• (пусто)")
    lines.append("")
    lines.append("Команда:")
    lines.append("лига: {страна} | {лига} | 1")
    return _truncate_telegram("\n".join(lines))


def _render_matches_page(user_id: int, country: str, league: str, page: int, per_page: int = 15) -> str:
    idx = _build_index_for_user(user_id)["countries"]
    ids = (idx.get(country) or {}).get(league) or []
    total = len(ids)
    if total == 0:
        return "Матчей не найдено."

    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = ids[start : start + per_page]

    lines = [f"🏳️ {country}", f"🏆 {league}", f"Страница {page}/{pages}", ""]
    for mid in chunk:
        meta = (_MATCH_CACHE_BY_USER.get(user_id) or {}).get(mid) or {}
        title = meta.get("title") or f"Матч {mid}"
        status = meta.get("status") or ""
        score = meta.get("score") or ""
        start_time = meta.get("start_time") or ""
        s: List[str] = []
        s.append(f"• {title}")
        if start_time:
            s.append(f"  старт: {start_time}")
        if score:
            s.append(f"  счёт: {score}")
        if status:
            s.append(f"  статус: {status}")
        s.append(f"  id: {mid}")
        lines.append("\n".join(s))

    lines.append("")
    if pages > 1:
        prev_p = max(1, page - 1)
        next_p = min(pages, page + 1)
        lines.append(f"Листать: лига: {country} | {league} | {prev_p}   /   {next_p}")
    lines.append("Открыть матч: матч <id>")
    return _truncate_telegram("\n".join(lines))


def _parse_nav_country(text_raw: str) -> Optional[str]:
    m = re.match(r"^(страна)\s*:\s*(.+)$", (text_raw or "").strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(2).strip()


def _parse_nav_league(text_raw: str) -> Optional[Tuple[str, str, int]]:
    m = re.match(r"^(лига)\s*:\s*(.+)$", (text_raw or "").strip(), flags=re.IGNORECASE)
    if not m:
        return None

    tail = m.group(2).strip()
    if "|" in tail:
        parts = [p.strip() for p in tail.split("|")]
    elif "/" in tail:
        parts = [p.strip() for p in tail.split("/")]
    else:
        return None

    if len(parts) < 2:
        return None

    country = parts[0]
    league = parts[1]
    page = 1
    if len(parts) >= 3 and parts[2]:
        try:
            page = int(re.findall(r"\d+", parts[2])[0])
        except Exception:
            page = 1
    return country, league, page


# -----------------------------
# API: матчи / матч
# -----------------------------
async def _format_matches_today_api(user_id: int, sport_slug: str) -> str:
    from .integrations.sport_api import SportAPIClient, SportAPIError

    def _infer_country_from_league(league: str) -> str:
        """
        Эвристика: угадываем страну по названию лиги.
        Можно расширять безболезненно.
        """
        lg = (league or "").strip().lower()
        if not lg:
            return ""

        MAP = {
            # Russia
            "khl": "Russia",
            "вхл": "Russia",
            "vhl": "Russia",
            "мхл": "Russia",
            "mhl": "Russia",

            # USA / Canada
            "nhl": "USA",
            "ahl": "USA",
            "echl": "USA",
            "whl": "Canada",
            "ohl": "Canada",
            "qmjhl": "Canada",
            "university league": "USA",

            # Europe
            "shl": "Sweden",
            "liiga": "Finland",
            "del": "Germany",
            "national league": "Switzerland",
            "swiss league": "Switzerland",
            "extraliga": "Czech Republic",
            "tipsport extraliga": "Czech Republic",
            "slovak extraliga": "Slovakia",
            "icehl": "Austria",

            # International
            "iihf": "International",
            "champions hockey league": "International",
            "world championship": "International",
            "chl": "International",
        }

        for k, v in MAP.items():
            if k in lg:
                return v
        return ""

    sport_slug = (sport_slug or "").strip().lower()
    if sport_slug not in API_SPORTS_LABELS:
        return (
            "Не понял спорт.\n"
            "Варианты: football, ice-hockey, basketball, tennis, table-tennis, esports"
        )

    today = _msk_today_date()
    title = API_SPORTS_LABELS.get(sport_slug, sport_slug)

    try:
        api = SportAPIClient()
        matches = await api.matches_by_date(sport_slug, today)
        if not matches:
            raise SportAPIError(f"matches_by_date empty for {sport_slug} {today.isoformat()}")
    except SportAPIError as e:
        return _truncate_telegram(
            "\n".join(
                [
                    f"🏟 Матчи сегодня (по МСК) — {title}",
                    f"Дата: {today.isoformat()}",
                    "",
                    "Не удалось получить матчи из API.",
                    f"Причина: {str(e)[:240]}",
                ]
            )
        )
    except Exception:
        logger.exception("Sport API error")
        return _truncate_telegram(
            "\n".join(
                [
                    f"🏟 Матчи сегодня (по МСК) — {title}",
                    f"Дата: {today.isoformat()}",
                    "",
                    "Не удалось получить матчи (ошибка сервера).",
                ]
            )
        )

    # ---- кеш матчей пользователя ----
    _MATCH_CACHE_BY_USER[user_id] = {}

    for m in matches:
        league = getattr(m, "league", "") or ""

        # 1) пробуем достать страну напрямую
        country = (
            getattr(m, "country", "") or
            getattr(m, "league_country", "") or
            getattr(m, "leagueCountry", "") or
            getattr(m, "country_name", "")
        ).strip()

        # 2) если не получилось — угадываем по лиге
        if not country:
            country = _infer_country_from_league(league)

        # 3) если всё ещё пусто — считаем International
        if not country:
            country = "International"

        _MATCH_CACHE_BY_USER[user_id][str(m.id)] = {
            "sport": getattr(m, "sport_slug", sport_slug),
            "title": getattr(m, "title", f"Матч {m.id}"),
            "league": league,
            "status": getattr(m, "status", ""),
            "start_time": getattr(m, "start_time", ""),
            "score": getattr(m, "score", "") or "",
            "odds_base": getattr(m, "odds_base", None),
            "country": country,
            "raw": getattr(m, "raw", None),
        }

    # Log odds_base availability in the loaded match list
    _ob_count = sum(
        1 for mm in _MATCH_CACHE_BY_USER[user_id].values()
        if mm.get("odds_base") and isinstance(mm.get("odds_base"), (dict, list))
    )
    _ns_count = sum(
        1 for mm in _MATCH_CACHE_BY_USER[user_id].values()
        if str(mm.get("status", "")).lower() in ("notstarted", "ns", "not started", "scheduled", "")
    )
    logger.info(
        "matches_by_date: %d total, %d have odds_base (dict/list), %d are NS/scheduled",
        len(matches), _ob_count, _ns_count,
    )
    # Log first 3 matches with their oddsBase type for debugging
    for _i, (_mid, _mm) in enumerate(list(_MATCH_CACHE_BY_USER[user_id].items())[:3]):
        _ob = _mm.get("odds_base")
        _raw_ob = (_mm.get("raw") or {}).get("oddsBase") if _mm.get("raw") else None
        logger.info(
            "MATCH_LIST[%d] id=%s status=%s odds_base=%s raw.oddsBase=%s(%s)",
            _i, _mid,
            str(_mm.get("status", ""))[:15],
            type(_ob).__name__,
            type(_raw_ob).__name__,
            repr(_raw_ob)[:120] if _raw_ob else "None",
        )

    return _render_countries(user_id, title, today.isoformat())




def _extract_date_from_start_time(start_time: str) -> Optional[date]:
    if not start_time:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(start_time))
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1)).date()
    except Exception:
        return None


async def _refresh_match_from_day_list(sport_slug: str, match_id: str, day: date) -> Optional[Dict[str, Any]]:
    try:
        from .integrations.sport_api import SportAPIClient

        api = SportAPIClient()
        matches = await api.matches_by_date(sport_slug, day)
        for m in matches:
            if str(getattr(m, "id", "")) == str(match_id):
                return {
                    "sport": getattr(m, "sport_slug", sport_slug),
                    "title": getattr(m, "title", f"Матч {match_id}"),
                    "league": getattr(m, "league", ""),
                    "status": getattr(m, "status", ""),
                    "start_time": getattr(m, "start_time", ""),
                    "score": normalize_score(getattr(m, "score", "")),
                    "odds_base": getattr(m, "odds_base", None),
                    "country": getattr(m, "country", "") if hasattr(m, "country") else "",
                    "raw": getattr(m, "raw", None),
                }
    except Exception:
        logger.exception("_refresh_match_from_day_list failed")
    return None


def normalize_score(raw_score: Any) -> str:
    """Normalize basketball-style dict scores into readable format.

    Handles:
      - Normal strings: "119:98" → "119:98"
      - Dict scores: {'quarter_1': 35, 'total': 119} → "119"
      - Stringified dicts: "{'quarter_1': 35, 'total': 119}:..." → "119:98 (35:20, 25:23, ...)"
    """
    if raw_score is None:
        return ""
    # If it's an actual dict (e.g. from raw API), extract total
    if isinstance(raw_score, dict):
        total = raw_score.get("total")
        return str(total) if total is not None else ""

    s = str(raw_score).strip()
    if not s:
        return ""

    # Fast path: if it looks like a normal score (digits:digits), return as-is
    if re.match(r'^\d+:\d+', s):
        return s

    # Detect stringified dict(s): "{'quarter_1': ...}:{'quarter_1': ...}"
    if "quarter_" in s or "'total'" in s or '"total"' in s:
        try:
            # Try to parse the two sides separated by "}:{"
            # The raw string looks like: "{'quarter_1': 35, 'total': 119}:{'quarter_1': 20, 'total': 98}"
            parts = re.split(r'\}:\{', s)
            if len(parts) == 2:
                import ast
                home_d = ast.literal_eval(parts[0] + '}')
                away_d = ast.literal_eval('{' + parts[1])
                h_total = home_d.get('total')
                a_total = away_d.get('total')
                if h_total is not None and a_total is not None:
                    # Build quarter breakdown
                    quarters = []
                    for q in range(1, 10):
                        h_q = home_d.get(f'quarter_{q}')
                        a_q = away_d.get(f'quarter_{q}')
                        if h_q is not None and a_q is not None:
                            quarters.append(f"{h_q}:{a_q}")
                    h_ot = home_d.get('over_time')
                    a_ot = away_d.get('over_time')
                    if h_ot is not None and a_ot is not None and (h_ot or a_ot):
                        quarters.append(f"{h_ot}:{a_ot}")
                    result = f"{h_total}:{a_total}"
                    if quarters:
                        result += f" ({', '.join(quarters)})"
                    return result
        except Exception:
            pass

    return s


def _format_status_and_score(status: str, score: str) -> Tuple[str, str]:
    """Return (status_label, score_label) in Russian-friendly form.

    We keep it short for Telegram cards:
      - ✅ Завершён
      - 🟢 Идёт
      - ⏳ Скоро
      - ⏸ Перенесён / Отменён
    """
    s = (status or "").strip().lower()
    sc = normalize_score(score)

    # Normalize a few common vendor statuses
    is_live = any(x in s for x in ("live", "1h", "2h", "3h", "ot", "so", "in progress", "playing"))
    is_finished = any(x in s for x in ("finished", "ft", "ended", "closed", "final"))
    is_postponed = any(x in s for x in ("postponed", "delayed", "suspended"))
    is_canceled = any(x in s for x in ("canceled", "cancelled"))
    is_scheduled = any(x in s for x in ("not started", "ns", "scheduled", "upcoming", "tbd"))

    if is_live:
        status_label = "🟢 Идёт"
    elif is_finished:
        status_label = "✅ Завершён"
    elif is_postponed:
        status_label = "⏸ Перенесён"
    elif is_canceled:
        status_label = "⛔ Отменён"
    elif is_scheduled or not s:
        status_label = "⏳ Скоро"
    else:
        # Fallback: show original status (uppercased) but without noise
        status_label = status.strip().upper()[:32] if status else ""

    # Score label
    score_label = sc
    if score_label and is_finished:
        # If provider doesn't include FT marker, add a short Russian one
        if "ft" not in score_label.lower() and "ф" not in score_label.lower():
            score_label = f"{score_label} (ФТ)"
    return status_label, score_label

def _live_no_change_text(match_meta: Dict[str, Any], action: str) -> str:
    title = str(match_meta.get("title") or "Матч").strip()
    league = str(match_meta.get("league") or "").strip()
    country = str(match_meta.get("country") or "").strip()
    status = str(match_meta.get("status") or "").strip()
    score = str(match_meta.get("score") or "").strip()
    status_label, score_label = _format_status_and_score(status, score)

    lines = [
        "🟢 LIVE",
        "Нет новых данных — показываю последний результат.",
        "",
        f"{title}",
    ]
    if league and country:
        lines.append(f"{league} • {country}")
    elif league:
        lines.append(league)
    elif country:
        lines.append(country)
    lines.append(f"Статус: {status_label}")
    lines.append(f"Счёт: {score_label}")
    if action == "pro":
        lines.append("")
        lines.append("LIVE PRO: новые данные не поступили.")
    return "\n".join(lines)


async def _get_match_context(user_id: int, match_id: str) -> Dict[str, Any]:
    match_id = str(match_id).strip()
    cached = (_MATCH_CACHE_BY_USER.get(user_id) or {}).get(match_id)
    sport = (_ACTIVE_SPORT_BY_USER.get(user_id) or "").strip().lower()

    def _is_live_done(st: str) -> Tuple[bool, bool]:
        s = (st or "").strip().lower()
        is_live = s in {"live", "inprogress", "in_progress"}
        is_done = s in {"finished", "ended"}
        return is_live, is_done

    if cached:
        c_status = str(cached.get("status") or "").strip()
        c_score = str(cached.get("score") or "").strip()
        is_live, is_done = _is_live_done(c_status)

        need_refresh = (is_live or is_done) and (not c_score)
        if not c_status:
            need_refresh = True

        if need_refresh and sport:
            merged = dict(cached)

            try:
                from .integrations.sport_api import SportAPIClient

                api = SportAPIClient()
                d = await api.match_details(sport, match_id)
                # --- DEBUG: inspect raw LIVE payload from SportAPI (if provided in MatchDTO.raw) ---
                raw = getattr(d, "raw", None) or {}
                if not isinstance(raw, dict):
                    raw = {"__non_dict_raw__": type(raw).__name__}
                logger.warning("LIVE RAW keys=%s", list(raw.keys())[:80])
                for k in ("statistics", "stats", "events", "incidents", "periods", "time", "clock", "penalties", "shots"):
                    if k in raw:
                        logger.warning("RAW has %s type=%s", k, type(raw.get(k)).__name__)
                # ------------------------------------------------------------------------------
                merged.update(
                    {
                        "sport": getattr(d, "sport_slug", merged.get("sport") or sport),
                        "title": getattr(d, "title", merged.get("title") or f"Матч {match_id}"),
                        "league": getattr(d, "league", merged.get("league") or ""),
                        "status": getattr(d, "status", merged.get("status") or ""),
                        "start_time": getattr(d, "start_time", merged.get("start_time") or ""),
                        "score": getattr(d, "score", merged.get("score") or ""),
                        "odds_base": getattr(d, "odds_base", None) or merged.get("odds_base"),
                        "country": getattr(d, "country", merged.get("country") or "")
                        if hasattr(d, "country")
                        else merged.get("country") or "",
                        "raw": getattr(d, "raw", None) or merged.get("raw"),
                    }
                )
            except Exception as _md_err:
                logger.exception("match_details refresh failed; will try day-list refresh")
                if sport != "tennis":  # tennis match_details fails often — known bug, no alert
                    try:
                        from .alerting import send_alert
                        import asyncio
                        asyncio.ensure_future(send_alert(
                            "WARNING", "parsing.match_details_refresh", _md_err,
                            user_id=user_id, context={"match_id": match_id, "sport": sport},
                        ))
                    except Exception:
                        pass

            if (not str(merged.get("score") or "").strip()) or (not str(merged.get("status") or "").strip()):
                day = _extract_date_from_start_time(str(merged.get("start_time") or "")) or _msk_today_date()
                refreshed = await _refresh_match_from_day_list(sport, match_id, day)
                if refreshed:
                    _saved_odds_base = merged.get("odds_base")
                    _saved_raw = merged.get("raw")
                    merged.update(refreshed)
                    # Don't overwrite cached odds_base/raw with None
                    if not merged.get("odds_base") and _saved_odds_base:
                        merged["odds_base"] = _saved_odds_base
                    if not merged.get("raw") and _saved_raw:
                        merged["raw"] = _saved_raw

            (_MATCH_CACHE_BY_USER.setdefault(user_id, {}))[match_id] = merged
            return dict(merged, id=match_id)

        return dict(cached, id=match_id)

    if sport:
        try:
            from .integrations.sport_api import SportAPIClient

            api = SportAPIClient()
            d = await api.match_details(sport, match_id)
            # --- DEBUG: inspect raw LIVE payload from SportAPI (if provided in MatchDTO.raw) ---
            raw = getattr(d, "raw", None) or {}
            if not isinstance(raw, dict):
                raw = {"__non_dict_raw__": type(raw).__name__}
            logger.warning("LIVE RAW keys=%s", list(raw.keys())[:80])
            for k in ("statistics", "stats", "events", "incidents", "periods", "time", "clock", "penalties", "shots"):
                if k in raw:
                    logger.warning("RAW has %s type=%s", k, type(raw.get(k)).__name__)
            # ------------------------------------------------------------------------------
            return {
                "id": match_id,
                "sport": getattr(d, "sport_slug", sport),
                "title": getattr(d, "title", f"Матч {match_id}"),
                "league": getattr(d, "league", ""),
                "status": getattr(d, "status", ""),
                "start_time": getattr(d, "start_time", ""),
                "score": getattr(d, "score", ""),
                "odds_base": getattr(d, "odds_base", None),
                "country": getattr(d, "country", "") if hasattr(d, "country") else "",
                "raw": getattr(d, "raw", None),
            }
        except Exception as _cm_err:
            logger.exception("match_details on cache miss failed; will try day-list refresh")
            if sport != "tennis":  # tennis match_details fails often — known bug, no alert
                try:
                    from .alerting import send_alert
                    import asyncio
                    asyncio.ensure_future(send_alert(
                        "WARNING", "parsing.match_details_cache_miss", _cm_err,
                        user_id=user_id, context={"match_id": match_id, "sport": sport},
                    ))
                except Exception:
                    pass

        refreshed = await _refresh_match_from_day_list(sport, match_id, _msk_today_date())
        if refreshed:
            return dict(refreshed, id=match_id)

    return {
        "id": match_id,
        "sport": sport or "",
        "title": f"Матч {match_id}",
        "league": "",
        "status": "",
        "start_time": "",
        "score": "",
        "odds_base": None,
        "country": "",
    }


def _oddsbase_snapshot(match_meta: Dict[str, Any], mode: str) -> Dict[str, Any]:
    ob = match_meta.get("odds_base")
    if not isinstance(ob, dict):
        return {"odds": {"present": False}}

    markets = ob.get("markets")
    if not isinstance(markets, list):
        return {"odds": {"present": True, "markets": []}}

    max_markets = 3 if (mode or "").lower() == "live" else 5
    max_choices = 3 if (mode or "").lower() == "live" else 5

    if (mode or "").lower() != "live":
        slim: List[Dict[str, Any]] = []
        for m in markets[:max_markets]:
            if not isinstance(m, dict):
                continue
            mm = {"name": m.get("name"), "marketId": m.get("marketId")}
            ch = m.get("choices")
            if isinstance(ch, list):
                mm["choices"] = [
                    {"name": c.get("name"), "odd": c.get("odd"), "change": c.get("change")}
                    for c in ch[:max_choices]
                    if isinstance(c, dict)
                ]
            slim.append(mm)
        return {"odds": {"present": True, "markets": slim}}

    slim_live: List[Dict[str, Any]] = []
    for m in markets[:max_markets]:
        if not isinstance(m, dict):
            continue
        mm = {"name": m.get("name")}
        ch = m.get("choices")
        if isinstance(ch, list):
            mm["choices"] = [{"name": c.get("name"), "change": c.get("change")} for c in ch[:max_choices] if isinstance(c, dict)]
        slim_live.append(mm)
    return {"odds": {"present": True, "markets": slim_live}}


# -----------------------------
# UI prompt + render
# -----------------------------

def _ui_json_schema(*, mode: str, action: str) -> str:
    """Return the JSON schema (as a string) used by the UI LLM response.

    We keep schemas as triple-quoted strings so we can embed them directly into
    prompts. This helper centralizes the routing logic and prevents NameError
    regressions when prompt builders change.
    """
    m = (mode or "").strip().lower()
    a = (action or "").strip().lower()

    # LIVE PRO is a special richer schema.
    if m == "live" and a in {"pro", "live_pro", "livepro"}:
        return _SCHEMA_UI_LIVE_PRO

    # Default LIVE schema for live/refresh/overview.
    if m == "live":
        return _SCHEMA_UI_LIVE

    # PRE schema for everything else.
    return _SCHEMA_UI_PRE


async def _enrich_match_meta(
    match_meta: Dict[str, Any],
    sport_slug: str,
    mode: str,
) -> Dict[str, Any]:
    """
    Enrich match_meta with external data from Odds API, Sports API, RSS.
    Adds '_enriched' key with context text for LLM prompts.
    Never raises — returns original match_meta on any error.
    """
    try:
        from .data_collector import collect_match_data
        from .prompt_builder import build_enriched_context_text

        title = str(match_meta.get("title") or "")
        parts = title.split(" — ") if " — " in title else title.split(" - ")
        home_team = parts[0].strip() if len(parts) >= 2 else title
        away_team = parts[1].strip() if len(parts) >= 2 else ""

        status = str(match_meta.get("status") or "").upper()
        raw = match_meta.get("raw") or {}

        # Extract team IDs if available (support multiple API formats)
        home_team_id = None
        away_team_id = None
        teams = raw.get("teams") or {}
        if isinstance(teams, dict):
            h = teams.get("home") or {}
            a = teams.get("away") or {}
            home_team_id = h.get("id") if isinstance(h, dict) else None
            away_team_id = a.get("id") if isinstance(a, dict) else None
        # Fallback: homeTeam/awayTeam format (primary API)
        if not home_team_id:
            ht = raw.get("homeTeam") or raw.get("home_team") or {}
            if isinstance(ht, dict):
                home_team_id = ht.get("id")
        if not away_team_id:
            at = raw.get("awayTeam") or raw.get("away_team") or {}
            if isinstance(at, dict):
                away_team_id = at.get("id")
        # Fallback: fixture.teams (football api-sports format)
        if not home_team_id and not away_team_id:
            fixture_teams = (raw.get("fixture") or {}).get("teams") or {}
            if not fixture_teams:
                fixture_teams = raw.get("teams") or {}
            if isinstance(fixture_teams, dict):
                fh = fixture_teams.get("home") or {}
                fa = fixture_teams.get("away") or {}
                if isinstance(fh, dict) and not home_team_id:
                    home_team_id = fh.get("id")
                if isinstance(fa, dict) and not away_team_id:
                    away_team_id = fa.get("id")

        # Extract league_id from raw data (api-sports.io includes league.id)
        league_id = None
        league_obj = raw.get("league") or {}
        if isinstance(league_obj, dict):
            league_id = league_obj.get("id")
        if not league_id:
            # Football: fixture.league.id
            fixture_league = (raw.get("fixture") or {}).get("league") or {}
            if isinstance(fixture_league, dict):
                league_id = fixture_league.get("id")
        if league_id is not None:
            try:
                league_id = int(league_id)
            except (ValueError, TypeError):
                league_id = None

        _mid = str(match_meta.get("id") or match_meta.get("match_id") or "")

        # Extract odds_base: try match_meta.odds_base first, then raw.oddsBase
        _odds_base = match_meta.get("odds_base")
        if not _odds_base or not isinstance(_odds_base, (dict, list)):
            _odds_base = raw.get("oddsBase") or raw.get("odds_base") or raw.get("odds")
        if _odds_base and isinstance(_odds_base, (dict, list)):
            import json as _json
            try:
                _ob_str = _json.dumps(_odds_base, ensure_ascii=False, default=str)
                logger.info("ODDS_BASE_RAW match=%s type=%s len_or_keys=%s JSON=%.800s",
                            _mid, type(_odds_base).__name__,
                            list(_odds_base.keys())[:10] if isinstance(_odds_base, dict) else len(_odds_base),
                            _ob_str)
            except Exception:
                logger.info("ODDS_BASE_RAW match=%s type=%s", _mid, type(_odds_base).__name__)
        else:
            _raw_ob_val = raw.get("oddsBase")
            logger.info(
                "ODDS_BASE_RAW match=%s → None | "
                "meta.odds_base=%s raw.oddsBase=%s(%s) "
                "status=%s",
                _mid,
                type(match_meta.get("odds_base")).__name__,
                type(_raw_ob_val).__name__,
                repr(_raw_ob_val)[:200],
                str(match_meta.get("status", ""))[:20],
            )

        ctx = await collect_match_data(
            match_id=_mid,
            home_team=home_team,
            away_team=away_team,
            league=str(match_meta.get("league") or ""),
            country=str(match_meta.get("country") or ""),
            start_time=str(match_meta.get("start_time") or ""),
            status=status,
            sport_slug=sport_slug,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            odds_base=_odds_base,
            league_id=league_id,
        )

        enriched_text = build_enriched_context_text(ctx)
        if enriched_text:
            match_meta["_enriched"] = enriched_text
            match_meta["_data_completeness"] = ctx.data_completeness()
            logger.info(
                "Enriched match %s: completeness=%d%%",
                _mid, ctx.data_completeness()
            )
        else:
            logger.warning(
                "Enrichment empty for match %s: odds=%s form=%s h2h=%s news=%d team_ids=%s/%s",
                _mid, ctx.has_odds(), ctx.has_form(), ctx.has_h2h(),
                len(ctx.news), home_team_id, away_team_id,
            )

    except Exception:
        logger.exception("_enrich_match_meta failed — continuing without enrichment")

    return match_meta


def _build_ui_prompt(
    match_meta: Dict[str, Any],
    mode: str,
    action: str,
    prev_snapshot: Optional[Dict[str, Any]],
    cur_snapshot: Optional[Dict[str, Any]],
) -> str:
    # PRO LIVE mode: используем hybrid engine (features + signals)
    if mode == "live" and action == "pro":
        return _build_pro_live_prompt(match_meta, prev_snapshot, cur_snapshot)

    schema = _ui_json_schema(mode=mode, action=action)
    cur = cur_snapshot or {}
    prev = prev_snapshot or {}

    guard = (
        "Ты — профессиональный спортивный аналитик.\n"
        "ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:\n"
        "1. Используй ТОЛЬКО данные из промпта. Не выдумывай кэфы, форму, составы.\n"
        "2. Каждый аргумент в key_facts подкрепляй КОНКРЕТНОЙ ЦИФРОЙ из данных "
        "(например: 'кэф 1.72', 'форма 6W-2L', 'H2H 4-1 в пользу хозяев', 'серия 3 победы').\n"
        "3. ЗАПРЕЩЕНЫ пустые фразы: 'данные неполные', 'возможны сюрпризы', "
        "'проверьте самостоятельно', 'составы неизвестны', 'высокая конкуренция', "
        "'равные шансы' — если есть реальные данные, используй их.\n"
        "4. recommendation: дай конкретный выбор (П1/П2/X/ТБ/ТМ). "
        "Оставь пустым ТОЛЬКО если данных < 30%.\n"
        "5. confidence: 55–60% = 1 слабый фактор; 65–70% = 2 фактора; "
        "75–80% = 3 фактора совпадают; 82–85% = форма+H2H+линия — все совпадают.\n"
        "6. Пиши по-русски, деловой стиль.\n"
        "7. Верни только JSON по схеме ниже."
    )

    if mode == "pre":
        focus = (
            "Фокус PRE-анализа (до матча):\n"
            "— ФОРМА: кто в лучшей серии, голы за/против, дома/гости рекорд\n"
            "— H2H: общий счёт встреч, средний тотал, кто выигрывает чаще\n"
            "— ЛИНИЯ: кэфы и движение (кэф упал → рынок ставит сюда)\n"
            "— ВЫВОД: recommendation на основе совпадения форма+H2H+линия\n"
            "— Если матч уже идёт/завершён — честно укажи это в summary"
        )
    else:
        focus = (
            "Фокус LIVE: опиши текущую ситуацию и импульс (momentum), "
            "что мониторить в ближайшие 5–10 минут/следующий период, "
            "и ключевые риски. "
            "Если статус/счёт есть во входе — отрази их."
        )

    depth = "Объём: 4–6 конкретных key_facts, 1–2 риска. Каждый факт — цифра или конкретный аргумент."

    # Remove internal keys from match_meta before sending to LLM
    meta_clean = {k: v for k, v in match_meta.items() if not k.startswith("_")}
    payload = {
        "match": meta_clean,
        "snapshot": cur,
        "prev_snapshot": prev,
        "mode": mode,
        "action": action,
    }

    # Enriched data from external APIs (Odds, Form, H2H, News)
    enriched = match_meta.get("_enriched") or ""
    enrichment_block = ""
    if enriched:
        completeness = match_meta.get("_data_completeness", 0)
        enrichment_block = (
            f"\n\nДОПОЛНИТЕЛЬНЫЕ ДАННЫЕ (из внешних источников, полнота {completeness}%):\n"
            + enriched
            + "\n\nИспользуй эти данные для конкретных цифр в анализе."
        )

    return (
        guard
        + "\n\n"
        + focus
        + "\n"
        + depth
        + "\n\n"
        + "JSON-схема (верни объект строго по ней):\n"
        + schema
        + "\n\n"
        + "Входные данные (JSON):\n"
        + json.dumps(payload, ensure_ascii=False)
        + enrichment_block
    )


def _build_pro_live_prompt(
    match_meta: Dict[str, Any],
    prev_snapshot: Optional[Dict[str, Any]],
    cur_snapshot: Optional[Dict[str, Any]],
) -> str:
    """Hybrid PRO prompt: features + rule engine signals + LLM структурирование."""
    try:
        from .live_features import extract_live_features, build_live_signals, format_features_for_prompt
        raw = match_meta.get("raw") or {}
        odds_raw = (cur_snapshot or {}).get("odds") or {}
        if isinstance(odds_raw, dict) and odds_raw.get("markets"):
            pass  # уже норм
        else:
            odds_raw = match_meta.get("odds_base") or {}
        features = extract_live_features(raw, odds_raw, match_meta)
        signals = build_live_signals(features)
        features_text = format_features_for_prompt(features, signals, match_meta)
    except Exception:
        logger.exception("PRO live_features extraction failed, using basic context")
        features_text = "Статистические данные недоступны — используй только переданный контекст матча."
        signals = {}

    title = str(match_meta.get("title") or "Матч").strip()
    league = str(match_meta.get("league") or "").strip()
    status = str(match_meta.get("status") or "").strip()
    score = str(match_meta.get("score") or "").strip()

    system_guard = (
        "Ты профессиональный live-аналитик.\n"
        "Правила:\n"
        "– Используй ТОЛЬКО переданные цифры и сигналы, не придумывай данные\n"
        "– Не пересказывай очевидное (счёт, время) без аналитики\n"
        "– Не пиши общие фразы ('стоит следить', 'возможно')\n"
        "– Дай максимум 2–3 конкретных сценария\n"
        "– Обязательно добавь риск-план (условие отмены сценария)\n"
        "– Пиши по-русски, деловой стиль, коротко\n"
        "– Верни только JSON по схеме ниже"
    )

    schema = _SCHEMA_UI_LIVE_PRO

    # Enriched external data (Odds API, Form, H2H, News)
    enriched = match_meta.get("_enriched") or ""

    context_block = "\n".join(filter(None, [
        f"Матч: {title}",
        f"Лига: {league}" if league else "",
        f"Статус: {status}" if status else "",
        f"Счёт: {score}" if score else "",
        "",
        features_text,
        "",
        enriched if enriched else "",
    ]))

    return (
        system_guard
        + "\n\n"
        + "JSON-схема:\n"
        + schema
        + "\n\n"
        + "Данные матча:\n"
        + context_block
    )
_SCHEMA_UI_PRE = """
{
  "title": "string — заголовок с командами и лигой",
  "summary": "string — 2-3 предложения с конкретными фактами и цифрами из данных",
  "recommendation": "string — П1 / П2 / X / ТБ N.N / ТМ N.N — пусто только если данных < 30%",
  "confidence": "integer — 55..85 процентов, 0 если данных недостаточно",
  "key_facts": ["string — конкретный аргумент с цифрой: кэф, форма, H2H, серия"],
  "risks": ["string — конкретный риск с условием"],
  "disclaimer": "string"
}
""".strip()

_SCHEMA_UI_LIVE = """
{
  "title": "string",
  "summary": "string",
  "context": ["string"],
  "live_state": "string",
  "key_events": ["string"],
  "insights": ["string"],
  "risks": ["string"],
  "disclaimer": "string"
}
""".strip()

_SCHEMA_UI_LIVE_PRO = """
{
  "title": "string",
  "summary": "string",
  "context": ["string"],
  "live_state": "string",
  "key_events": ["string"],
  "pro_angles": ["string"],
  "bets": ["string"],
  "risks": ["string"],
  "disclaimer": "string"
}
""".strip()


def _schema_prompt(mode: str, action: str) -> str:
    if mode == "live" and action == "pro":
        return _SCHEMA_UI_LIVE_PRO
    if mode == "live":
        return _SCHEMA_UI_LIVE
    return _SCHEMA_UI_PRE


def _render_ui_json(analysis: Dict[str, Any], *, mode: str, action: str) -> str:
    if not analysis:
        return "AI недоступен."

    is_pro_live = (mode == "live" and action == "pro")
    is_pre = (mode == "pre")
    title = str(analysis.get("title") or "").strip() or ("🟢 LIVE" if mode == "live" else "📊 Обзор")
    lines: list[str] = [title]

    if analysis.get("summary"):
        lines += ["", str(analysis["summary"]).strip()]

    # ── PRE: рекомендация + уверенность (главный блок) ─────────────────────
    if is_pre:
        rec = str(analysis.get("recommendation") or "").strip()
        conf_raw = analysis.get("confidence")
        try:
            conf = int(conf_raw) if conf_raw is not None else 0
        except (ValueError, TypeError):
            conf = 0

        if rec and conf > 0:
            lines += ["", f"🎯 Рекомендация: {rec}"]
            lines.append(f"📊 Уверенность: {conf}%")
        elif rec:
            lines += ["", f"🎯 Рекомендация: {rec}"]

    # PRO LIVE: live_state — текущее состояние матча
    if is_pro_live and analysis.get("live_state"):
        lines += ["", f"📍 {str(analysis['live_state']).strip()}"]

    # ── PRE: key_facts (аргументы с цифрами) ────────────────────────────────
    if is_pre:
        key_facts = analysis.get("key_facts") or []
        # Fallback: also support old "context" / "insights" fields
        if not key_facts:
            key_facts = analysis.get("context") or analysis.get("insights") or []
        if key_facts:
            lines += ["", "✅ Аргументы"]
            for f in key_facts[:6]:
                lines.append(f"• {f}")
    else:
        # LIVE: context block
        ctx = analysis.get("context") or []
        if ctx:
            lines.append("")
            for x in ctx[:6]:
                lines.append(f"• {x}")

    # PRO LIVE: key_events — ключевые события
    key_events = analysis.get("key_events") or []
    if is_pro_live and key_events:
        lines += ["", "⚡ Ключевые события"]
        for e in key_events[:5]:
            lines.append(f"• {e}")

    # PRO LIVE: pro_angles — сценарии/углы
    pro_angles = analysis.get("pro_angles") or []
    if is_pro_live and pro_angles:
        lines += ["", "🎯 Сценарии"]
        for a in pro_angles[:4]:
            lines.append(f"• {a}")

    # PRO LIVE: bets — ставки/рекомендации
    bets = analysis.get("bets") or []
    if is_pro_live and bets:
        lines += ["", "💡 Рекомендации"]
        for b in bets[:4]:
            lines.append(f"• {b}")

    # Regular LIVE: insights
    insights = analysis.get("insights") or []
    if not is_pro_live and not is_pre and insights:
        lines += ["", "💡 Инсайты"]
        for i in insights[:4]:
            lines.append(f"• {i}")

    risks = analysis.get("risks") or []
    if risks:
        lines.append("")
        lines.append("⚠️ Риски")
        for r in risks[:6]:
            lines.append(f"• {r}")

    disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()
    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


def _hash_cache_key(match_id: str, sport_slug: str, mode: str, action: str, cur_snap: Dict[str, Any]) -> str:
    payload = {"m": str(match_id), "sport": str(sport_slug or ""), "mode": str(mode or ""), "action": str(action or ""), "cur": cur_snap}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _pro_teaser_footer() -> str:
    return (
        "\n\n"
        "🔒 Полный LIVE PRO доступен в подписке.\n"
        "Что даст PRO:\n"
        "• уровни/триггеры/сценарии\n"
        "• риск-план и условия отмены сценария\n"
        "• меньше воды — больше структуры\n\n"
        "Нажми: ⭐ Оформить PRO"
    )


async def _run_ui_llm(user_id: int, match_id: str, mode: str, action: str) -> str:
    user_id = int(user_id or 0)
    match_id = str(match_id or "").strip()
    mode = (mode or "pre").lower()
    action = (action or "overview").lower()

    if mode not in {"pre", "live"}:
        mode = "pre"
    if action not in {"overview", "pro", "refresh"}:
        action = "overview"

    is_refresh = action == "refresh"

    match_meta = await _get_match_context(user_id, match_id)
    sport_slug = str(match_meta.get("sport") or "ice-hockey").strip().lower()

    # ── Handle refresh: cooldown + cache invalidation ──
    if is_refresh:
        now_ts = time.time()
        last_refresh = _USER_REFRESH_COOLDOWN.get(user_id, 0)
        if now_ts - last_refresh < _REFRESH_COOLDOWN_S:
            remaining = int(_REFRESH_COOLDOWN_S - (now_ts - last_refresh))
            return f"⏳ Подожди {remaining} сек перед обновлением."
        _USER_REFRESH_COOLDOWN[user_id] = now_ts
        # Clear caches to force fresh data
        _LIVE_DATA_CACHE.pop(f"{sport_slug}:{match_id}", None)
        _LIVE_RENDER_BY_MATCH.pop((match_id, "overview"), None)
        _LIVE_RENDER_BY_MATCH.pop((match_id, "pro"), None)
        action = "overview"

    # ── Fetch LIVE extras (statistics, events) for live matches ──
    status_raw = str(match_meta.get("status") or "").strip()
    if mode == "live" and _is_live_match(status_raw):
        extras = await _fetch_live_extras(sport_slug, match_id)
        if extras and extras.get("sources"):
            raw = match_meta.get("raw") or {}
            if not isinstance(raw, dict):
                raw = {}
            raw = _merge_extras_into_raw(dict(raw), extras)
            match_meta["raw"] = raw

    # ── Enrich with external data (Odds API + Sports API + RSS) ──
    match_meta = await _enrich_match_meta(match_meta, sport_slug, mode)

    prev_snap = _LIVE_SNAPSHOT_BY_MATCH.get(match_id) if mode == "live" else None
    cur_snap = _oddsbase_snapshot(match_meta, mode=mode)

    if mode == "live" and prev_snap is not None and cur_snap == prev_snap:
        cached = _LIVE_RENDER_BY_MATCH.get((match_id, action))
        if cached:
            return cached
        # No cached render for THIS action (e.g. user switched from overview → pro).
        # Fall through to LLM — don't short-circuit on first request for a new action.

    trial_banner = ""
    if mode == "live" and action == "pro":
        # DB may be unavailable — never let DB errors kill LIVE response
        try:
            user_is_pro = is_pro(user_id)
        except Exception:
            logger.exception("is_pro DB error — defaulting to non-PRO, trial allowed through")
            user_is_pro = False

        if not user_is_pro:
            trial_used = False
            try:
                with db_session() as session:
                    trial_used = _trial_live_used(session, user_id)
                    if not trial_used:
                        if _consume_trial_live(session, user_id):
                            trial_banner = "🎁 Trial LIVE PRO активирован (1/1)\n\n"
                        # _consume_trial_live returns False ONLY on DB error (never "already used")
                        # Do NOT set trial_used = True on DB error — let user through
            except Exception:
                logger.exception("DB unavailable for trial LIVE check — allowing LIVE PRO (trial not deducted)")
                trial_used = False  # DB down → пропускаем пользователя, не ломаем ответ

        if not user_is_pro and trial_used:
            teaser_action = "overview"
            prompt = _build_ui_prompt(match_meta, mode, teaser_action, prev_snap, cur_snap)
            cache_key = f"v16:ui:{sport_slug}:{match_id}:{mode}:{teaser_action}"
            schema = "ui_live"
            ttl_s = TTL_LIVE_S

            analysis, meta = await analyze_with_llm_cached_safe(
                prompt,
                cache_key=cache_key,
                schema=schema,
                ttl_s=int(ttl_s),
                user_id=user_id,
                mode=mode,
                snapshot=cur_snap,
            )
            _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
            if isinstance(analysis, str):
                base_txt = analysis
            else:
                base_txt = _render_ui_json(analysis, mode=mode, action=teaser_action)
            out = _truncate_telegram(base_txt) + _pro_teaser_footer()

            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            # Only cache teaser if LLM succeeded — don't cache fallback renders
            if not (meta or {}).get("used_fallback"):
                _LIVE_RENDER_BY_MATCH[(match_id, teaser_action)] = out
            return out

    # ── PRO LIVE: deterministic engine (no LLM) ──────────────────────────────
    if mode == "live" and action == "pro":
        try:
            from .pro_engine import run_pro_engine
            prev_eng_snap = None
            if isinstance(prev_snap, dict) and prev_snap.get("_pro_engine"):
                prev_eng_snap = prev_snap["_pro_engine"]
            out = run_pro_engine(
                match_meta,
                prev_snapshot={"_pro_engine": prev_eng_snap} if prev_eng_snap else None,
                cur_snapshot={"odds": match_meta.get("odds_base") or {}},
            )
        except Exception:
            logger.exception("run_pro_engine failed — fallback to LLM")
            out = None

        if out:
            if trial_banner:
                out = trial_banner + out
            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            _LIVE_RENDER_BY_MATCH[(match_id, action)] = out
            return out
        # If PRO engine fails fall through to LLM below

    # ── LLM path (overview / pre / fallback) ─────────────────────────────────
    prompt = _build_ui_prompt(match_meta, mode, action, prev_snap, cur_snap)

    if mode == "live":
        cache_key = f"v16:ui:{sport_slug}:{match_id}:{mode}:{action}"
    else:
        h = _hash_cache_key(match_id, sport_slug, mode, action, cur_snap)
        cache_key = f"v16:ui:{sport_slug}:{match_id}:{mode}:{action}:{h}"

    if mode == "live" and action == "pro":
        schema = "ui_live_pro"
        ttl_s = TTL_LIVE_PRO_S
    else:
        schema = "ui_live" if mode == "live" else "ui_pre"
        ttl_s = TTL_LIVE_S if mode == "live" else TTL_PRE_S

    analysis, meta = await analyze_with_llm_cached_safe(
        prompt,
        cache_key=cache_key,
        schema=schema,
        ttl_s=int(ttl_s),
        user_id=user_id,
        mode=mode,
        snapshot=cur_snap,
    )

    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
    # analysis may be a plain string (fallback) or a dict (normal LLM result)
    if isinstance(analysis, str):
        out = analysis
    else:
        out = _render_ui_json(analysis, mode=mode, action=action)

    if trial_banner and mode == "live" and action == "pro":
        out = trial_banner + out

    if mode == "live":
        _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
        # Only cache successful renders — never cache fallback ("AI недоступен") text
        if not (meta or {}).get("used_fallback"):
            _LIVE_RENDER_BY_MATCH[(match_id, action)] = out

    return out


# ============================================================
# Telegram / HTTP entrypoint
# ============================================================
# ============================================================
# Telegram / HTTP entrypoint
# ============================================================
async def run_dialog_agent(user_id: int, text: str) -> str:
    user_id = int(user_id or 0)
    raw = (text or "").strip()
    norm = raw.lower().strip()

    if norm in {"ping", "/ping", "ping!", "пинг"}:
        if ADMIN_TELEGRAM_ID and user_id != ADMIN_TELEGRAM_ID:
            pass  # non-admin: fall through to normal handling
        else:
            return "pong ✅"

    # UI callbacks from Telegram come like: UI:<match_id>:<pre|live>:<overview|pro|refresh|markets>
    if norm.startswith("ui:"):
        parts = raw.split(":")
        if len(parts) >= 4:
            match_id = parts[1].strip()
            mode = parts[2].strip().lower()
            action = parts[3].strip().lower()
            try:
                return await _run_ui_llm(user_id, match_id, mode, action)
            except Exception as e:
                logger.exception("ui command failed")
                try:
                    from .alerting import send_alert
                    await send_alert("ERROR", "parsing.ui_command", e, user_id=user_id, context={"match_id": match_id, "mode": mode, "action": action})
                except Exception:
                    pass
                return f"⚠️ Ошибка UI: {type(e).__name__}: {str(e)[:160]}"
        return "⚠️ Формат: UI:<match_id>:<pre|live>:<overview|pro|refresh|markets>"

    # Debug/manual CLI format
    if norm.startswith("ui match "):
        parts = raw.split()
        if len(parts) >= 5:
            match_id = parts[2].strip()
            mode = parts[3].strip().lower()
            action = parts[4].strip().lower()
            try:
                return await _run_ui_llm(user_id, match_id, mode, action)
            except Exception as e:
                logger.exception("ui command failed")
                try:
                    from .alerting import send_alert
                    await send_alert("ERROR", "parsing.ui_command", e, user_id=user_id, context={"match_id": match_id, "mode": mode, "action": action})
                except Exception:
                    pass
                return f"⚠️ Ошибка UI: {type(e).__name__}: {str(e)[:160]}"
        return "⚠️ Формат: ui match <match_id> <pre|live> <overview|pro|refresh|markets>"

    if "матчи сегодня" in norm:
        sport_slug = "ice-hockey"
        parts = norm.split()
        if parts:
            last = parts[-1].strip()
            if last in {"ice-hockey", "hockey", "football", "basketball", "tennis", "table-tennis", "esports", "mma", "volleyball", "handball", "baseball"}:
                sport_slug = "ice-hockey" if last == "hockey" else last
        try:
            _ACTIVE_SPORT_BY_USER[user_id] = sport_slug
            return await _format_matches_today_api(user_id, sport_slug)
        except Exception as e:
            logger.exception("format matches today failed")
            try:
                from .alerting import send_alert
                await send_alert("ERROR", "parsing.format_matches_today", e, user_id=user_id, context={"sport": sport_slug})
            except Exception:
                pass
            return f"⚠️ Не удалось получить матчи: {type(e).__name__}: {str(e)[:200]}"

    c = _parse_nav_country(raw)
    if c:
        _ACTIVE_COUNTRY_BY_USER[user_id] = c
        _ACTIVE_LEAGUE_BY_USER[user_id] = ""
        _ACTIVE_PAGE_BY_USER[user_id] = 1
        return _render_leagues(user_id, c)

    lg = _parse_nav_league(raw)
    if lg:
        country, league, page = lg
        _ACTIVE_COUNTRY_BY_USER[user_id] = country
        _ACTIVE_LEAGUE_BY_USER[user_id] = league
        _ACTIVE_PAGE_BY_USER[user_id] = page
        return _render_matches_page(user_id, country, league, page)

    if norm.startswith("матч "):
        match_id = raw.split(" ", 1)[1].strip()
        if not match_id:
            return "⚠️ Укажи id матча: матч <match_id>"

        sport_slug = "ice-hockey"
        try:
            idx = _build_index_for_user(user_id)
            mm = (idx.get("match_meta") or {}).get(str(match_id))
            if isinstance(mm, dict) and mm.get("sport"):
                sport_slug = str(mm["sport"]).strip() or sport_slug
        except Exception:
            pass

        try:
            from .integrations.sport_api import SportAPIClient

            api = SportAPIClient()
            dto = await api.match_details(sport_slug, match_id)

            title = (dto.title or f"Матч {match_id}").strip()
            league = (dto.league or "").strip()
            country = (dto.country or "").strip()
            status = (dto.status or "").strip()
            score = (dto.score or "").strip()
            start_time = (dto.start_time or "").strip()

            try:
                st, sc = _format_status_and_score(status, score)
            except Exception:
                st, sc = status, score

            lines = [f"🏟 {title}"]
            if league:
                lines.append(f"🏆 {league}")
            if country and country != "Other":
                lines.append(f"🏳️ {country}")
            if start_time:
                lines.append(f"🕒 {start_time}")
            if st or sc:
                lines.append(f"{st} {sc}".strip())

            lines.append("")
            return "\n".join(lines).strip()

        except Exception as e:
            logger.exception("match details failed")
            try:
                from .alerting import send_alert
                await send_alert("ERROR", "parsing.match_details_main", e, user_id=user_id, context={"match_id": match_id, "sport": sport_slug})
            except Exception:
                pass
            return f"⚠️ Не удалось открыть матч {match_id}: {type(e).__name__}: {str(e)[:200]}"

    try:
        from .ui_text import MENU_HINT_TEXT
        return MENU_HINT_TEXT
    except Exception:
        return (
            "Воспользуйся кнопками меню:\n\n"
            "🎯 Охотник — топ матчи дня\n"
            "📊 Анализ матчей\n"
            "⭐ PRO режим\n\n"
            "Нажми /start чтобы открыть меню."
        )
