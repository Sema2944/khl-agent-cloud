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
from .pro_db import is_pro

logger = logging.getLogger(__name__)

# --- LLM cooldown (anti-spam on 429 / insufficient_quota) ---
_LLM_DISABLED_UNTIL_TS = 0
_LLM_DISABLED_REASON = ""


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

    # «Псевдо-AI» советы (универсально и безопасно)
    if mode.startswith("pre"):
        lines += [
            "",
            "Что проверить перед стартом:",
            "• составы/травмы/вратари (для хоккея — критично)",
            "• мотивацию (турнирная ситуация, серия, дерби)",
            "• движение линии за 60–120 минут до матча",
            "",
            "Риск-стоп:",
            "• резкое падение коэффициентов без новостей — лучше пропустить",
        ]
    else:
        lines += [
            "",
            "На что смотреть в LIVE:",
            "• темп и моменты (счёт может не отражать игру)",
            "• удаления/карточки и кто доминирует",
            "• реакция коэффициентов на ключевые события",
            "",
            "Риск-стоп:",
            "• не «догонять» после серии минусов",
            "• если данных мало — дождаться паузы/следующего отрезка",
        ]

    return {
        "title": title,
        "summary": "\n".join(lines).strip(),
        "risks": [
            "Не ставьте на эмоциях и не увеличивайте сумму после неудач.",
            "Если информации мало или линия ведёт себя странно — пропускайте матч.",
        ],
        "cta": "Хочешь — нажми «Обновить LIVE» через пару минут, данные могут подтянуться.",
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
            text = (
                "• AI временно недоступен — показываю базовую справку.

"
                "Риски
"
                "• Недостаточно данных для детального разбора.

"
                "Аналитический материал, не является рекомендацией."
            )
        meta = {
            "ok": False,
            "reason": reason,
            "mode": mode,
            "action": action,
            "match_id": match_id,
            "has_snapshot": bool(cur_snap),
        }
        return text, meta

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
        }

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
                    "score": getattr(m, "score", ""),
                    "odds_base": getattr(m, "odds_base", None),
                    "country": getattr(m, "country", "") if hasattr(m, "country") else "",
                }
    except Exception:
        logger.exception("_refresh_match_from_day_list failed")
    return None


def _format_status_and_score(status: str, score: str) -> Tuple[str, str]:
    """Return (status_label, score_label) in Russian-friendly form.

    We keep it short for Telegram cards:
      - ✅ Завершён
      - 🟢 Идёт
      - ⏳ Скоро
      - ⏸ Перенесён / Отменён
    """
    s = (status or "").strip().lower()
    sc = (score or "").strip()

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
                merged.update(
                    {
                        "sport": getattr(d, "sport_slug", merged.get("sport") or sport),
                        "title": getattr(d, "title", merged.get("title") or f"Матч {match_id}"),
                        "league": getattr(d, "league", merged.get("league") or ""),
                        "status": getattr(d, "status", merged.get("status") or ""),
                        "start_time": getattr(d, "start_time", merged.get("start_time") or ""),
                        "score": getattr(d, "score", merged.get("score") or ""),
                        "odds_base": getattr(d, "odds_base", merged.get("odds_base")),
                        "country": getattr(d, "country", merged.get("country") or "")
                        if hasattr(d, "country")
                        else merged.get("country") or "",
                    }
                )
            except Exception:
                logger.exception("match_details refresh failed; will try day-list refresh")

            if (not str(merged.get("score") or "").strip()) or (not str(merged.get("status") or "").strip()):
                day = _extract_date_from_start_time(str(merged.get("start_time") or "")) or _msk_today_date()
                refreshed = await _refresh_match_from_day_list(sport, match_id, day)
                if refreshed:
                    merged.update(refreshed)

            (_MATCH_CACHE_BY_USER.setdefault(user_id, {}))[match_id] = merged
            return dict(merged, id=match_id)

        return dict(cached, id=match_id)

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


def _build_ui_prompt(
    match_meta: Dict[str, Any],
    mode: str,
    action: str,
    prev_snapshot: Optional[Dict[str, Any]],
    cur_snapshot: Optional[Dict[str, Any]],
) -> str:
    # IMPORTANT:
    # - Return ONLY JSON (no markdown, no extra text).
    # - Write in Russian.
    # - Do NOT give direct betting commands (no "ставь", "бет", "точно зайдёт"). Only аналитика/сценарии.
    # - If данных мало, честно скажи и заполни поля коротко.
    schema = _ui_json_schema(mode=mode, action=action)
    cur = cur_snapshot or {}
    prev = prev_snapshot or {}

    guard = (
        "Ты — спортивный аналитик (хоккей/футбол и др.). "
        "Твоя задача: дать краткий, структурированный разбор матча на основе переданных данных. "
        "Не выдумывай факты (составы/травмы/коэффициенты), если их нет во входе. "
        "Пиши по-русски, деловой стиль, без воды. "
        "Верни только JSON по схеме ниже."
    )

    # Mode hints
    if mode == "pre":
        focus = (
            "Фокус PRE: что важно проверить ДО матча и какие сценарии возможны. "
            "Если есть стартовое время — упомяни окно 60–120 минут до начала для проверки составов/вратарей. "
            "Если матч уже идёт/завершён — честно укажи это в summary."
        )
    else:
        focus = (
            "Фокус LIVE: опиши текущую ситуацию и импульс (momentum), "
            "что мониторить в ближайшие 5–10 минут/следующий период, "
            "и ключевые риски. "
            "Если статус/счёт есть во входе — отрази их."
        )

    if action == "pro":
        depth = (
            "Глубже (PRO): добавь 3–6 пунктов 'pro_angles' — конкретные наблюдаемые триггеры/сигналы "
            "(темп, удаления, броски, вратари, серия, усталость, тренды по периодам). "
            "В поле 'bets' пиши как 'сценарии/варианты' без призывов и без уверенных обещаний."
        )
    else:
        depth = (
            "Коротко (overview): 5–10 строк суммарно, упор на практический чек-лист и риски."
        )

    payload = {
        "match": match_meta,
        "snapshot": cur,
        "prev_snapshot": prev,
        "mode": mode,
        "action": action,
    }

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
    )
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

    title = str(analysis.get("title") or "").strip() or ("🟢 LIVE" if mode == "live" else "📊 Обзор")
    lines: list[str] = [title]

    if analysis.get("summary"):
        lines += ["", str(analysis["summary"]).strip()]

    ctx = analysis.get("context") or []
    if ctx:
        lines.append("")
        for x in ctx[:6]:
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
    if action == "refresh":
        action = "overview"

    match_meta = await _get_match_context(user_id, match_id)
    sport_slug = str(match_meta.get("sport") or "ice-hockey").strip().lower()

    prev_snap = _LIVE_SNAPSHOT_BY_MATCH.get(match_id) if mode == "live" else None
    cur_snap = _oddsbase_snapshot(match_meta, mode=mode)

    if mode == "live" and prev_snap is not None and cur_snap == prev_snap:
        cached = _LIVE_RENDER_BY_MATCH.get((match_id, action))
        if cached:
            return cached
        out = _live_no_change_text(match_meta, action)
        _LIVE_RENDER_BY_MATCH[(match_id, action)] = out
        return out

    trial_banner = ""
    if mode == "live" and action == "pro" and not is_pro(user_id):
        trial_used = False
        with db_session() as session:
            trial_used = _trial_live_used(session, user_id)
            if not trial_used:
                if _consume_trial_live(session, user_id):
                    trial_banner = "🎁 Trial LIVE PRO активирован (1/1)\n\n"
                else:
                    trial_used = True

        if trial_used:
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
            base_txt = _render_ui_json(analysis, mode=mode, action=teaser_action)
            out = _truncate_telegram(base_txt) + _pro_teaser_footer()

            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            _LIVE_RENDER_BY_MATCH[(match_id, teaser_action)] = out
            return out

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
    out = _render_ui_json(analysis, mode=mode, action=action)

    if trial_banner and mode == "live" and action == "pro":
        out = trial_banner + out

    if mode == "live":
        _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
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
                return f"⚠️ Ошибка UI: {type(e).__name__}: {str(e)[:160]}"
        return "⚠️ Формат: ui match <match_id> <pre|live> <overview|pro|refresh|markets>"

    if "матчи сегодня" in norm:
        sport_slug = "ice-hockey"
        parts = norm.split()
        if parts:
            last = parts[-1].strip()
            if last in {"ice-hockey", "hockey", "football", "basketball", "tennis", "table-tennis", "esports"}:
                sport_slug = "ice-hockey" if last == "hockey" else last
        try:
            _ACTIVE_SPORT_BY_USER[user_id] = sport_slug
            return await _format_matches_today_api(user_id, sport_slug)
        except Exception as e:
            logger.exception("format matches today failed")
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
            return f"⚠️ Не удалось открыть матч {match_id}: {type(e).__name__}: {str(e)[:200]}"

    if "стратег" in norm:
        try:
            return _format_expert_strategy_for_today()
        except Exception as e:
            logger.exception("strategy failed")
            return f"⚠️ Стратегия недоступна: {type(e).__name__}: {str(e)[:160]}"

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
        "• страна: <название>\n"
        "• лига: <страна> | <лига> | <страница>\n"
        "• матч <match_id>\n"
    )

