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
from .expert_db import ExpertStrategy
from .llm_client import analyze_with_llm_cached
import time

# --- LLM cooldown (anti-spam on 429 / insufficient_quota) ---
_LLM_DISABLED_UNTIL_TS = 0
_LLM_DISABLED_REASON = ""


def _is_quota_error(err: Exception) -> bool:
    s = str(err)
    return (
        "insufficient_quota" in s
        or "You exceeded your current quota" in s
        or "HTTP 429" in s
        or '"code": "insufficient_quota"' in s
    )


def _fallback_analysis(reason: str) -> dict:
    # Универсальный fallback (подходит для UI и для общего ответа)
    return {
        "title": "📊 Обзор рынков",
        "summary": "AI временно недоступен — показываю базовую справку.",
        "risks": ["Недостаточно данных для детального разбора."],
        "disclaimer": "Аналитический материал, не является рекомендацией.",
        "debug": {"llm_reason": reason},
    }


async def analyze_with_llm_cached_safe(*args, **kwargs):
    """
    Обертка над analyze_with_llm_cached:
    - если недавно был quota/429 -> не зовем OpenAI
    - если получили quota/429 -> ставим блок на N минут и возвращаем fallback
    """
    global _LLM_DISABLED_UNTIL_TS, _LLM_DISABLED_REASON

    now = int(time.time())
    if now < _LLM_DISABLED_UNTIL_TS:
        reason = _LLM_DISABLED_REASON or "llm_cooldown_active"
        return _fallback_analysis(reason), {"llm_disabled": True, "reason": reason}

    try:
        return await analyze_with_llm_cached(*args, **kwargs)
    except Exception as e:
        if _is_quota_error(e):
            _LLM_DISABLED_REASON = f"quota/429: {str(e)[:180]}"
            _LLM_DISABLED_UNTIL_TS = int(time.time()) + 20 * 60  # 20 минут
            return _fallback_analysis(_LLM_DISABLED_REASON), {"llm_disabled": True, "reason": _LLM_DISABLED_REASON}

        # любые другие ошибки НЕ прячем
        raise

from .pro_db import is_pro

logger = logging.getLogger(__name__)

ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()

MSK = ZoneInfo("Europe/Moscow")

LLM_PROMPT_PREFIX = (os.getenv("LLM_PROMPT_PREFIX") or "").strip()
if not LLM_PROMPT_PREFIX:
    LLM_PROMPT_PREFIX = (
        "Ты дружелюбный, структурированный и безопасный спортивный аналитик.\n"
        "Оъясняй логику движения линии.\n"
        "НЕ предсказывай исход и НЕ давай советов.\n"
        "Пиши коротко, списками."
    )

# -----------------------------
# TTL policy for LLM caching
# -----------------------------
TTL_PRE_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())                 # prematch
TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "75").strip())            # LIVE (default 75s)
TTL_LIVE_PRO_S = int((os.getenv("LLM_CACHE_TTL_LIVE_PRO_S") or "75").strip())    # LIVE PRO (default 75s)

_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_ACTIVE_SPORT_BY_USER: Dict[int, str] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}

# LIVE snapshot should be GLOBAL PER MATCH
_LIVE_SNAPSHOT_BY_MATCH: Dict[str, Dict[str, Any]] = {}
_LIVE_RENDER_BY_MATCH: Dict[Tuple[str, str], str] = {}

# кеш матчей "сегодня" по пользователю: match_id -> meta
_MATCH_CACHE_BY_USER: Dict[int, Dict[str, Dict[str, Any]]] = {}

# навигация по списку матчей
_ACTIVE_COUNTRY_BY_USER: Dict[int, str] = {}
_ACTIVE_LEAGUE_BY_USER: Dict[int, str] = {}
_ACTIVE_PAGE_BY_USER: Dict[int, int] = {}

API_SPORTS_LABELS = {
    "football": "⚽ Футбол",
    "ice-hockey": "🏒 Хоккей",
    "basketball": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",
    "table-tennis": "🏓 Настольный теннис",
    "esports": "🎮 Киберспорт",
}

TELEGRAM_MAX_CHARS = 3800


def _now_ts() -> int:
    return int(time.time())


def _msk_today_date() -> date:
    return datetime.now(MSK).date()


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


def _md_safe_text(text: str) -> str:
    return _md_escape(text or "")


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
# Профиль / банк
# -----------------------------
def _format_profile_text(bank: Optional[float], stats: bets_db.UserStats) -> str:
    lines: list[str] = []
    lines.append("📊 Твой профиль")

    if bank is None:
        lines.append("Банк: ещё не задан")
        lines.append("Совет: задай банк командой: мой банк 100000")
    else:
        lines.append(f"Банк: {bank:,.0f}".replace(",", " "))

    lines.append("")
    lines.append(f"Всего ставок: {stats.total_bets}")
    lines.append(f"Рассчитано ставок (без возвратов): {stats.settled_bets}")
    lines.append(f"Возвратов: {stats.pushes}")
    lines.append(f"Winrate: {stats.winrate:.1f}%")
    lines.append(f"ROI: {stats.roi:.1f}%")
    lines.append(f"PnL: {stats.pnl:+.0f}")
    lines.append(f"Объём ставок: {stats.total_stake:.0f}")
    lines.append("")
    lines.append("Это упрощённая статистика по всем твоим ставкам.")
    return "\n".join(lines)


def _parse_bank_set(message: str) -> Optional[float]:
    nums = re.findall(r"(\d+[ \d]*)", (message or "").replace("\u00a0", " "))
    if not nums:
        return None
    num = nums[0].replace(" ", "")
    try:
        return float(num)
    except ValueError:
        return None


# -----------------------------
# Экспертная стратегия
# -----------------------------
def _get_strategy_row(session: Session, day) -> Optional[ExpertStrategy]:
    st = (
        select(ExpertStrategy)
        .where(ExpertStrategy.date == day)
        .order_by(ExpertStrategy.updated_at.desc())
    )
    return session.exec(st).first()


def _format_expert_strategy_for_today() -> str:
    today = _msk_today_date()

    text_ = ""
    date_label = today.isoformat()

    try:
        with db_session() as session:
            row = _get_strategy_row(session, today)
            if row and row.text:
                text_ = row.text
                date_label = row.date.isoformat()
    except Exception:
        logger.exception("expert_strategy table missing or db error (fallback to env)")

    if not text_ and EXPERT_STRATEGY_TEXT:
        text_ = EXPERT_STRATEGY_TEXT
        date_label = EXPERT_STRATEGY_DATE or date_label

    if not text_:
        return (
            "👤 Стратегия эксперта на сегодня (по МСК)\n"
            "Пока не опубликована.\n\n"
            "Если ты админ — обнови командой:\n"
            "админ стратегия: <текст>"
        )

    return "\n".join(
        [
            "👤 Стратегия эксперта на сегодня (по МСК)",
            f"Дата: {date_label}",
            "",
            text_,
            "",
            "Дисклеймер: это аналитическая заметка, не призыв к действию.",
        ]
    )


def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
    if ADMIN_TELEGRAM_ID <= 0:
        return False, "ADMIN_TELEGRAM_ID не задан в окружении backend."
    if user_id != ADMIN_TELEGRAM_ID:
        return False, "Доступ запрещён."

    m = re.match(
        r"админ\s+стратегия\s*:\s*(.+)$",
        (raw_text or "").strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return False, "Неверный формат. Пример: админ стратегия: текст..."

    new_text = m.group(1).strip()
    if not new_text:
        return False, "Пустой текст стратегии."

    today = _msk_today_date()
    now = datetime.utcnow()

    with db_session() as session:
        row = _get_strategy_row(session, today)
        if row is None:
            row = ExpertStrategy(
                date=today,
                text=new_text,
                created_at=now,
                updated_at=now,
                updated_by=user_id,
            )
            session.add(row)
        else:
            row.text = new_text
            row.updated_at = now
            row.updated_by = user_id
            session.add(row)
        session.commit()

    return True, "✅ Стратегия обновлена (по МСК)."


# -----------------------------
# Группировка матчей: страна -> лига -> матчи
# -----------------------------
def _norm_key(s: Any) -> str:
    s = (str(s or "")).strip()
    return s if s else "Other"


def _build_index_for_user(user_id: int) -> Dict[str, Any]:
    cache = _MATCH_CACHE_BY_USER.get(user_id) or {}
    idx: Dict[str, Dict[str, List[str]]] = {}
    for mid, meta in cache.items():
        country = _norm_key(meta.get("country") or meta.get("league_country") or "Other")
        league = _norm_key(meta.get("league") or "Other")
        idx.setdefault(country, {}).setdefault(league, []).append(mid)

    for c, leagues in idx.items():
        for lg, ids in leagues.items():
            ids.sort(key=lambda _id: str((cache.get(_id) or {}).get("start_time") or ""))
    return idx


def _render_countries(user_id: int, sport_title: str, today_iso: str) -> str:
    idx = _build_index_for_user(user_id)
    items: List[Tuple[str, int]] = []
    for country, leagues in idx.items():
        n = sum(len(v) for v in leagues.values())
        items.append((country, n))
    items.sort(key=lambda x: x[1], reverse=True)

    lines = [
        f"🏟 Матчи сегодня (по МСК) — {sport_title}",
        f"Дата: {today_iso}",
        "",
        "Выбери страну:",
    ]
    for c, n in items[:10]:
        lines.append(f"• {c} ({n})")
    if len(items) > 10:
        rest = sum(n for _, n in items[10:])
        lines.append(f"• Другие ({rest})")

    lines.append("")
    lines.append("Команды навигации:")
    lines.append("• страна: <название>")
    lines.append("• лига: <страна> | <лига> | <страница?>")
    return _truncate_telegram("\n".join(lines))


def _render_leagues(user_id: int, country: str) -> str:
    idx = _build_index_for_user(user_id)
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
    idx = _build_index_for_user(user_id)
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
# API: матчи / матч / oddsBase
# -----------------------------
async def _format_matches_today_api(user_id: int, sport_slug: str) -> str:
    from .integrations.sport_api import SportAPIClient, SportAPIError

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

    _MATCH_CACHE_BY_USER[user_id] = {}
    for m in matches:
        _MATCH_CACHE_BY_USER[user_id][str(m.id)] = {
            "sport": getattr(m, "sport_slug", sport_slug),
            "title": getattr(m, "title", f"Матч {m.id}"),
            "league": getattr(m, "league", ""),
            "status": getattr(m, "status", ""),
            "start_time": getattr(m, "start_time", ""),
            "score": getattr(m, "score", "") or "",
            "odds_base": getattr(m, "odds_base", None),
            "country": getattr(m, "country", "") or "",
        }

    _ACTIVE_COUNTRY_BY_USER[user_id] = ""
    _ACTIVE_LEAGUE_BY_USER[user_id] = ""
    _ACTIVE_PAGE_BY_USER[user_id] = 1

    return _render_countries(user_id, title, today.isoformat())


def _format_match_hub_text(
    match_id: str,
    *,
    title: str,
    league: str,
    country: str,
    sport_slug: str,
    status: str,
    start_time: str,
    score: str = "",
) -> str:
    status_label, score_label = _format_status_and_score(status, score)
    lines: list[str] = []
    lines.append("🏟 Матч")
    lines.append(f"{title}")
    if league and country:
        lines.append(f"Лига: {league} • {country}")
    elif league:
        lines.append(f"Лига: {league}")
    elif country:
        lines.append(f"Страна: {country}")
    if sport_slug:
        lines.append(f"Вид спорта: {API_SPORTS_LABELS.get(sport_slug, sport_slug)}")
    lines.append(f"Статус: {status_label}")
    if start_time:
        lines.append(f"Старт: {start_time}")
    lines.append(f"Счёт: {score_label}")
    lines.append(f"id: {_md_escape(match_id)}")
    lines.append("")
    lines.append("Выбери действие кнопками ниже 👇")
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)


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


async def _refresh_match_from_day_list(
    sport_slug: str,
    match_id: str,
    day: date,
) -> Optional[Dict[str, Any]]:
    """
    Fallback refresher: re-fetch day list and find match_id there.
    This avoids relying solely on match_details which can be flaky/404 on some providers.
    """
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
                    "score": getattr(m, "score", ""),
                    "odds_base": getattr(m, "odds_base", None),
                    "country": getattr(m, "country", "") if hasattr(m, "country") else "",
                }
    except Exception:
        logger.exception("_refresh_match_from_day_list failed")
    return None


def _format_status_and_score(status: str, score: str) -> Tuple[str, str]:
    s = (status or "").strip().lower()
    sc = (score or "").strip()

    is_live = s in {"live", "inprogress", "in_progress"}
    is_done = s in {"finished", "ended"}
    is_not_started = s in {"notstarted", "not_started", "scheduled", "fixture", "pending"}

    if is_live:
        return "LIVE", sc or "—"
    if is_done:
        return "FINISHED", f"{sc} (FT)" if sc else "—"
    if is_not_started or not s:
        return "NOT STARTED", "—"
    return s.upper(), sc or "—"


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
    """
    Контекст матча:
    1) берём из кеша (матчи сегодня)
    2) если LIVE/FINISHED и нет score/статус странный — пытаемся освежить:
       a) match_details()
       b) если оно не работает / 404 — refresh from day list (matches_by_date + поиск id)
    """
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
        # также если статус пустой — полезно освежить
        if not c_status:
            need_refresh = True

        if need_refresh and sport:
            merged = dict(cached)

            # 1) try match_details
            try:
                from .integrations.sport_api import SportAPIClient
                api = SportAPIClient()
                d = await api.match_details(sport, match_id)

                merged.update(
                    {
                        "sport": getattr(d, "sport_slug", merged.get("sport") or sport),
                        "title": getattr(d, "title", merged.get("title") or f"Матч {match_id}"),
                        "league": getattr(d, "league", merged.get("league") or ""),
                        "status": getattr(d, "status", merged.get("status") or ""),
                        "start_time": getattr(d, "start_time", merged.get("start_time") or ""),
                        "score": getattr(d, "score", merged.get("score") or ""),
                        "odds_base": getattr(d, "odds_base", merged.get("odds_base")),
                        "country": getattr(d, "country", merged.get("country") or "") if hasattr(d, "country") else merged.get("country") or "",
                    }
                )
            except Exception:
                logger.exception("match_details refresh failed; will try day-list refresh")

            # 2) day-list refresh if still no score/status
            if (not str(merged.get("score") or "").strip()) or (not str(merged.get("status") or "").strip()):
                day = _extract_date_from_start_time(str(merged.get("start_time") or "")) or _msk_today_date()
                refreshed = await _refresh_match_from_day_list(sport, match_id, day)
                if refreshed:
                    merged.update(refreshed)

            # update cache
            (_MATCH_CACHE_BY_USER.setdefault(user_id, {}))[match_id] = merged
            return dict(merged, id=match_id)

        return dict(cached, id=match_id)

    # cache miss: try match_details then day-list refresh (today)
    if sport:
        try:
            from .integrations.sport_api import SportAPIClient
            api = SportAPIClient()
            d = await api.match_details(sport, match_id)
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
            }
        except Exception:
            logger.exception("match_details on cache miss failed; will try day-list refresh")

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

    # LIVE: максимально лёгкий снапшот без odds
    slim_live: List[Dict[str, Any]] = []
    for m in markets[:max_markets]:
        if not isinstance(m, dict):
            continue
        mm = {"name": m.get("name")}
        ch = m.get("choices")
        if isinstance(ch, list):
            mm["choices"] = [
                {"name": c.get("name"), "change": c.get("change")}
                for c in ch[:max_choices]
                if isinstance(c, dict)
            ]
        slim_live.append(mm)
    return {"odds": {"present": True, "markets": slim_live}}


# -----------------------------
# UI / LLM
# -----------------------------
def _build_ui_prompt(
    match_meta: Dict[str, Any],
    mode: str,
    action: str,
    prev_snap: Optional[Dict[str, Any]],
    cur_snap: Dict[str, Any],
) -> str:
    """
    URGENT: trimmed LIVE prompt to reduce tokens.
    - less boilerplate
    - short schema
    - keep only essentials + slim snapshot
    """
    mode = (mode or "pre").lower()
    action = (action or "overview").lower()

    title = str(match_meta.get("title") or "Матч").strip()
    league = str(match_meta.get("league") or "").strip()
    sport = str(match_meta.get("sport") or "").strip()
    match_id = str(match_meta.get("id") or "").strip()
    status = str(match_meta.get("status") or "").strip()
    score = str(match_meta.get("score") or "").strip()

    head = [
        LLM_PROMPT_PREFIX,
        "",
        "Ограничения: без прогнозов/советов; не использовать слова: ставь/бери/выгодно/лучше/проход/гарантия/100%.",
        "Тон: кратко, списками, без воды.",
        "",
    ]

    ctx = [
        f"Матч: {title}",
        f"Лига: {league}" if league else "",
        f"Вид спорта: {sport}" if sport else "",
        f"Статус: {status}" if status else "",
        f"Счет: {score}" if score else "",
        f"id: {match_id}",
    ]
    ctx = [x for x in ctx if x]

    meta = [
        "",
        f"Режим: {mode}",
        f"Действие: {action}",
        "",
        "Текущий снапшот (cur):",
        json.dumps(cur_snap, ensure_ascii=False),
    ]

    if prev_snap is not None:
        meta += ["", "Предыдущий снапшот (prev):", json.dumps(prev_snap, ensure_ascii=False)]

    schema_hint = _schema_prompt(mode, action)
    return "\n".join(head + ctx + meta + ["", schema_hint])

# --- UI schemas (fallback, если схемы не объявлены выше) ---
# Нужны для _schema_prompt(); иначе NameError.
try:
    _SCHEMA_UI_PRE
except NameError:
    _SCHEMA_UI_PRE = """
{
  "title": "string",
  "summary": "string",
  "context": ["string"],
  "insights": ["string"],
  "risks": ["string"],
  "disclaimer": "string"
}
""".strip()

try:
    _SCHEMA_UI_LIVE
except NameError:
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

try:
    _SCHEMA_UI_LIVE_PRO
except NameError:
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


def _schema_to_json(schema: str) -> Dict[str, Any]:
    try:
        return json.loads(schema)
    except Exception:
        return {}


def _format_ui_json_to_text(
    analysis: Dict[str, Any],
    *,
    mode: str,
    action: str,
) -> str:
    title = str(analysis.get("title") or "").strip()
    summary = str(analysis.get("summary") or "").strip()
    risks = analysis.get("risks") or []
    disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()

    lines: list[str] = []
    if title:
        lines.append(title)
    if summary:
        lines.append("")
        lines.append(summary)

    ctx = analysis.get("context") or []
    if ctx:
        lines.append("")
        lines.append("Контекст")
        for x in ctx[:6]:
            lines.append(f"• {x}")

    if mode == "live" and action == "pro":
        pro = analysis.get("pro") or {}
        if isinstance(pro, dict):
            bias = str(pro.get("bias") or "").strip()
            if bias:
                lines.append("")
                lines.append("Смещение")
                lines.append(f"• {bias}")

            levels = pro.get("levels") or {}
            if isinstance(levels, dict):
                sup = levels.get("support") or []
                res = levels.get("resistance") or []
                if sup or res:
                    lines.append("")
                    lines.append("Уровни (интерпретация линии)")
                    for x in (sup[:3] if isinstance(sup, list) else []):
                        lines.append(f"• Поддержка: {x}")
                    for x in (res[:3] if isinstance(res, list) else []):
                        lines.append(f"• Сопротивление: {x}")

            triggers = pro.get("triggers") or []
            if triggers:
                lines.append("")
                lines.append("Триггеры")
                for x in triggers[:4]:
                    lines.append(f"• {x}")

            scenarios = pro.get("scenarios") or []
            if scenarios:
                lines.append("")
                lines.append("Сценарии")
                for s in scenarios[:3]:
                    if not isinstance(s, dict):
                        continue
                    nm = str(s.get("name") or "").strip() or "Сценарий"
                    iff = str(s.get("if") or "").strip()
                    thn = str(s.get("then") or "").strip()
                    lines.append(f"— {nm}")
                    if iff:
                        lines.append(f"  если: {iff}")
                    if thn:
                        lines.append(f"  то: {thn}")

            rp = pro.get("risk_plan") or []
            if rp:
                lines.append("")
                lines.append("Риск-план")
                for x in rp[:4]:
                    lines.append(f"• {x}")

    if risks:
        lines.append("")
        lines.append("Риски")
        for r in risks[:6]:
            lines.append(f"• {r}")

    lines.append("")
    lines.append(disclaimer)

    return "\n".join(lines)


def _render_ui_json(
    analysis: Dict[str, Any],
    *,
    mode: str,
    action: str,
) -> str:
    if not analysis:
        return "AI недоступен."

    title = str(analysis.get("title") or "").strip()
    if not title:
        title = "🟢 LIVE" if mode == "live" else "📊 Обзор"

    lines: list[str] = [title]

    if analysis.get("summary"):
        lines += ["", str(analysis["summary"]).strip()]

    ctx = analysis.get("context") or []
    if ctx:
        lines.append("")
        for x in ctx[:6]:
            lines.append(f"• {x}")

    kf = analysis.get("key_factors") or []
    if kf:
        lines.append("")
        lines.append("Факторы")
        for x in kf[:6]:
            lines.append(f"• {x}")

    ll = analysis.get("line_logic") or []
    if ll:
        lines.append("")
        lines.append("Логика линии")
        for x in ll[:6]:
            lines.append(f"• {x}")

    mk = analysis.get("markets") or []
    if mk:
        lines.append("")
        lines.append("Ключевые рынки")
        for item in mk[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "Market"))
            direction = str(item.get("direction", "unknown"))
            logic = str(item.get("logic", "")).strip()
            lines.append(f"— {name}: {direction}")
            if logic:
                lines.append(f"  {logic}")

    # PRO extra
    if mode == "live" and action == "pro":
        pro = analysis.get("pro") or {}
        if isinstance(pro, dict):
            bias = str(pro.get("bias") or "").strip()
            if bias:
                lines.append("")
                lines.append("Смещение")
                lines.append(f"• {bias}")

            levels = pro.get("levels") or {}
            if isinstance(levels, dict):
                sup = levels.get("support") or []
                res = levels.get("resistance") or []
                if sup or res:
                    lines.append("")
                    lines.append("Уровни (интерпретация линии)")
                    for x in (sup[:3] if isinstance(sup, list) else []):
                        lines.append(f"• Поддержка: {x}")
                    for x in (res[:3] if isinstance(res, list) else []):
                        lines.append(f"• Сопротивление: {x}")

            trig = pro.get("triggers") or []
            if isinstance(trig, list) and trig:
                lines.append("")
                lines.append("Триггеры")
                for x in trig[:4]:
                    lines.append(f"• {x}")

            scen = pro.get("scenarios") or []
            if isinstance(scen, list) and scen:
                lines.append("")
                lines.append("Сценарии")
                for s in scen[:3]:
                    if not isinstance(s, dict):
                        continue
                    nm = str(s.get("name") or "").strip() or "Сценарий"
                    iff = str(s.get("if") or "").strip()
                    thn = str(s.get("then") or "").strip()
                    lines.append(f"— {nm}")
                    if iff:
                        lines.append(f"  если: {iff}")
                    if thn:
                        lines.append(f"  то: {thn}")

            rp = pro.get("risk_plan") or []
            if isinstance(rp, list) and rp:
                lines.append("")
                lines.append("Риск-план")
                for x in rp[:4]:
                    lines.append(f"• {x}")

            notes = pro.get("notes") or []
            if isinstance(notes, list) and notes:
                lines.append("")
                lines.append("Заметки")
                for x in notes[:4]:
                    lines.append(f"• {x}")

    risks = analysis.get("risks") or []
    if risks:
        lines.append("")
        lines.append("Риски")
        for r in risks[:6]:
            lines.append(f"• {r}")

    disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()
    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


def _hash_cache_key(match_id: str, sport_slug: str, mode: str, action: str, cur_snap: Dict[str, Any]) -> str:
    payload = {
        "m": str(match_id),
        "sport": str(sport_slug or ""),
        "mode": str(mode or ""),
        "action": str(action or ""),
        "cur": cur_snap,
    }
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
    match_meta = await _get_match_context(user_id, match_id)

    sport_slug = str(match_meta.get("sport") or "").strip().lower()
    match_id = str(match_meta.get("id") or match_id).strip()
    mode = (mode or "pre").strip().lower()
    action = (action or "overview").strip().lower()

    cur_snap = {
        "status": match_meta.get("status"),
        "start_time": match_meta.get("start_time"),
        "score": match_meta.get("score"),
        **_oddsbase_snapshot(match_meta, mode),
    }

    prev_snap = None
    if mode == "live":
        prev_snap = _LIVE_SNAPSHOT_BY_MATCH.get(match_id)
        if action == "refresh":
            action = "overview"
        if prev_snap is not None and cur_snap == prev_snap:
            cached = _LIVE_RENDER_BY_MATCH.get((match_id, action))
            if cached:
                return cached
            no_change = _live_no_change_text(match_meta, action)
            _LAST_LLM_META_BY_USER[user_id] = {
                "provider": "local",
                "attempts": 0,
                "elapsed_ms": 0,
                "used_fallback": True,
                "last_error": "no_new_data",
                "cache": "no_change",
            }
            _LIVE_RENDER_BY_MATCH[(match_id, action)] = no_change
            return no_change

    # ---------- LIVE PRO gating + TRIAL ----------
    trial_banner = ""

    if mode == "live" and action == "pro" and not is_pro(user_id):
        trial_used = False
        with db_session() as session:
            trial_used = _trial_live_used(session, user_id)

            if not trial_used:
                ok = _consume_trial_live(session, user_id)
                if ok:
                    trial_banner = "🎁 Trial LIVE PRO активирован (1/1)\n\n"
                else:
                    trial_used = True

        if trial_used:
            teaser_action = "overview"
            prompt = _build_ui_prompt(match_meta, mode, teaser_action, prev_snap, cur_snap)

            # LIVE: стабильный cache_key без снапшот-хеша => меньше LLM вызовов / меньше TPM
            cache_key = f"v16:ui:{sport_slug}:{match_id}:{mode}:{teaser_action}"

                        if _llm_is_disabled():
                # сразу fallback без запроса к OpenAI
                return "AI временно недоступен — попробуй позже."

            try:
                analysis, meta = await analyze_with_llm_cached(
                    prompt,
                    cache_key=cache_key,
                    schema=schema,
                    ttl_s=int(ttl_s),
                    user_id=user_id,
                )
            except Exception as e:
                if _is_quota_error(e):
                    _llm_trip_disable(20)  # 20 минут не стучимся в OpenAI
                    return "AI временно недоступен (лимит/квота). Попробуй позже."
                raise

            _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})

            base_txt = _render_ui_json(analysis, mode=mode, action=teaser_action)
            out = _truncate_telegram(base_txt) + _pro_teaser_footer()
            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            _LIVE_RENDER_BY_MATCH[(match_id, teaser_action)] = out
            return out


        # trial активирован — продолжаем как PRO (ниже)

    # ---------- Normal / PRO ----------
    prompt = _build_ui_prompt(match_meta, mode, action, prev_snap, cur_snap)

    # LIVE: стабильный cache_key (без хеша); PRE: оставляем hash по снапшоту
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

    if _llm_is_disabled():
    # сразу fallback без запроса к OpenAI
    return "AI временно недоступен — попробуй позже."

try:
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=cache_key,
        schema=schema,
        ttl_s=int(ttl_s),
        user_id=user_id,
    )
except Exception as e:
    if _is_quota_error(e):
        _llm_trip_disable(20)  # 20 минут не стучимся в OpenAI
        return "AI временно недоступен (лимит/квота). Попробуй позже."
    raise

    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
    out = _render_ui_json(analysis, mode=mode, action=action)

    if trial_banner and mode == "live" and action == "pro":
        out = trial_banner + out

    if mode == "live":
        _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
        _LIVE_RENDER_BY_MATCH[(match_id, action)] = out

    return out
import asyncio

# --- REPLACE THIS WHOLE FUNCTION in src/parsing.py ---

# ============================================================
# Telegram / HTTP entrypoint
# ============================================================

async def run_dialog_agent(user_id: int, text: str) -> str:
    """
    Главный entrypoint для Telegram/HTTP.
    Поддерживает:
      - ping
      - матчи сегодня [sport_slug]
      - матч <match_id>
      - ui match <match_id> <pre|live> <overview|pro|refresh>
      - профиль
      - стратегия
    """
    user_id = int(user_id or 0)
    raw = (text or "").strip()
    norm = raw.lower().strip()

    # 0) healthcheck
    if norm in {"ping", "/ping", "ping!", "пинг"}:
        return "pong ✅"

    # 1) UI command from telegram_bot/app.py: "ui match <id> <mode> <action>"
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
                return f"⚠️ Ошибка UI: {type(e).__name__}: {str(e)[:160]}"
        return "⚠️ Формат: ui match <match_id> <pre|live> <overview|pro|refresh>"

    # 2) "матчи сегодня" (+ необязательный sport_slug)
    if "матчи сегодня" in norm:
        # пробуем вытащить slug последним токеном
        sport_slug = "ice-hockey"
        parts = norm.split()
        if parts:
            last = parts[-1].strip()
            # если последнее слово похоже на slug спорта — используем
            if last in {
                "ice-hockey",
                "hockey",
                "football",
                "basketball",
                "tennis",
                "table-tennis",
                "esports",
            }:
                # нормализуем alias
                sport_slug = "ice-hockey" if last == "hockey" else last

        try:
            return await _format_matches_today_api(user_id, sport_slug)
        except Exception as e:
            logger.exception("format matches today failed")
            return f"⚠️ Не удалось получить матчи: {type(e).__name__}: {str(e)[:200]}"

    # 3) "матч <id>"
    if norm.startswith("матч "):
        match_id = raw.split(" ", 1)[1].strip()
        if not match_id:
            return "⚠️ Укажи id матча: матч <match_id>"

        # определяем sport_slug из индекса (если матч уже был загружен списком)
        sport_slug = "ice-hockey"
        try:
            idx = _build_index_for_user(user_id)  # dict with cached entities
            mm = (idx.get("match_meta") or {}).get(str(match_id))
            if isinstance(mm, dict) and mm.get("sport_slug"):
                sport_slug = str(mm["sport_slug"]).strip() or sport_slug
        except Exception:
            pass

        # тянем details из API и рендерим хаб-текст
        try:
            from .integrations.sport_api import SportAPIClient

            api = SportAPIClient()
            dto = await api.match_details(sport_slug, match_id)

            # минимальный, но стабильный текст
            title = (dto.title or f"Матч {match_id}").strip()
            league = (dto.league or "").strip()
            country = (dto.country or "").strip()
            status = (dto.status or "").strip()
            score = (dto.score or "").strip()
            start_time = (dto.start_time or "").strip()

            # приводим score к адекватному виду (иногда там dict-строка)
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
            lines.append("Кнопки: PRE / LIVE / LIVE PRO / Обновить LIVE")

            return "\n".join(lines).strip()

        except Exception as e:
            logger.exception("match details failed")
            return f"⚠️ Не удалось открыть матч {match_id}: {type(e).__name__}: {str(e)[:200]}"

    # 4) профиль
    if "профиль" in norm:
        try:
            # если у тебя есть bank/stats — это место можно расширить,
            # но сейчас хотя бы не падаем
            return "📊 Профиль временно в разработке.\nНапиши: матчи сегодня"
        except Exception as e:
            logger.exception("profile failed")
            return f"⚠️ Профиль недоступен: {type(e).__name__}: {str(e)[:160]}"

    # 5) стратегия
    if "стратег" in norm:
        try:
            return _format_expert_strategy_for_today()
        except Exception as e:
            logger.exception("strategy failed")
            return f"⚠️ Стратегия недоступна: {type(e).__name__}: {str(e)[:160]}"

    # fallback: пробуем админ-обновление стратегии, иначе help
    try:
        ok, msg = _try_admin_update_strategy(user_id, raw)
        if ok:
            return msg
    except Exception:
        pass

    return (
        "Не понял команду.\n\n"
        "Доступно:\n"
        "• ping\n"
        "• матчи сегодня [ice-hockey|football|basketball|tennis|table-tennis|esports]\n"
        "• матч <match_id>\n"
    )



