# src/parsing.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy
from .llm_client import analyze_with_llm_cached

logger = logging.getLogger(__name__)

ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()

MSK = ZoneInfo("Europe/Moscow")

LLM_PROMPT_PREFIX = (os.getenv("LLM_PROMPT_PREFIX") or "").strip()
if not LLM_PROMPT_PREFIX:
    LLM_PROMPT_PREFIX = (
        "Ты дружелюбный, структурированный и безопасный спортивный аналитик.\n"
        "Твоя задача — объяснять коэффициенты и логику линии.\n"
        "НЕ предсказывай исход и НЕ давай советов по ставкам.\n"
        "Пиши коротко, списками."
    )

# -----------------------------
# TTL policy for LLM caching
# -----------------------------
TTL_PRE_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())  # 15 минут
TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "25").strip())  # 25 секунд

_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_ACTIVE_SPORT_BY_USER: Dict[int, str] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}

# LIVE snapshot should be GLOBAL PER MATCH (not per user) — иначе кеш развалится
_LIVE_SNAPSHOT_BY_MATCH: Dict[str, Dict[str, Any]] = {}

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

# чтобы Telegram не ругался Message is too long (ограничение ~4096)
TELEGRAM_MAX_CHARS = 3800


def _now_ts() -> int:
    return int(time.time())


def _msk_today_date():
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

    text = ""
    date_label = today.isoformat()

    # ✅ DB optional: если таблицы нет — не падаем
    try:
        with db_session() as session:
            row = _get_strategy_row(session, today)
            if row and row.text:
                text = row.text
                date_label = row.date.isoformat()
    except Exception:
        logger.exception("expert_strategy table missing or db error (fallback to env)")

    if not text and EXPERT_STRATEGY_TEXT:
        text = EXPERT_STRATEGY_TEXT
        date_label = EXPERT_STRATEGY_DATE or date_label

    if not text:
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
            text,
            "",
            "Дисклеймер: это аналитическая заметка, не призыв к ставке.",
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
    # idx[country][league] = [match_id,...]
    for mid, meta in cache.items():
        country = _norm_key(meta.get("country") or meta.get("league_country") or "Other")
        league = _norm_key(meta.get("league") or "Other")
        idx.setdefault(country, {}).setdefault(league, []).append(mid)

    # стабильный порядок внутри лиг: по start_time (если есть) иначе как пришло
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
    """
    Форматы:
      лига: Country | League | 1
      лига: Country | League
      лига: Country / League / 2
    """
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
            "score": getattr(m, "score", ""),
            "odds_base": getattr(m, "odds_base", None),
            "country": getattr(m, "country", "") if hasattr(m, "country") else "",
        }

    _ACTIVE_COUNTRY_BY_USER[user_id] = ""
    _ACTIVE_LEAGUE_BY_USER[user_id] = ""
    _ACTIVE_PAGE_BY_USER[user_id] = 1

    return _render_countries(user_id, title, today.isoformat())


def _fmt_status_ru(status: str) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "—"
    map_ru = {
        "not_started": "не начался",
        "notstarted": "не начался",
        "scheduled": "по расписанию",
        "live": "LIVE",
        "inprogress": "LIVE",
        "in_progress": "LIVE",
        "finished": "завершён",
        "ended": "завершён",
        "canceled": "отменён",
        "cancelled": "отменён",
        "postponed": "перенесён",
    }
    return map_ru.get(s, status)


def _format_match_hub_text(
    match_id: str,
    *,
    title: str,
    league: str,
    sport_slug: str,
    status: str,
    start_time: str,
    score: str = "",
) -> str:
    lines: list[str] = []
    lines.append("🏟 Матч")
    lines.append(f"{title}")
    if league:
        lines.append(f"Лига: {league}")
    if sport_slug:
        lines.append(f"Вид спорта: {API_SPORTS_LABELS.get(sport_slug, sport_slug)}")
    if status:
        lines.append(f"Статус: {_fmt_status_ru(status)}")
    if start_time:
        lines.append(f"Старт: {start_time}")
    if score:
        lines.append(f"Счёт: {score}")
    lines.append(f"id: {_md_escape(match_id)}")
    lines.append("")
    lines.append("Выбери действие кнопками ниже 👇")
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)


async def _get_match_context(user_id: int, match_id: str) -> Dict[str, Any]:
    match_id = str(match_id).strip()
    cached = (_MATCH_CACHE_BY_USER.get(user_id) or {}).get(match_id)
    if cached:
        return dict(cached, id=match_id)

    sport = (_ACTIVE_SPORT_BY_USER.get(user_id) or "").strip().lower()
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
            pass

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

    # ЖЁСТКО режем, чтобы LLM не таймаутил
    max_markets = 4 if (mode or "").lower() == "live" else 5
    max_choices = 4 if (mode or "").lower() == "live" else 5

    if (mode or "").lower() != "live":
        slim: List[Dict[str, Any]] = []
        for m in markets[:max_markets]:
            if not isinstance(m, dict):
                continue
            mm = {"name": m.get("name"), "marketId": m.get("marketId")}
            ch = m.get("choices")
            if isinstance(ch, list):
                mm["choices"] = [
                    {
                        "name": c.get("name"),
                        "odd": c.get("odd"),
                        "change": c.get("change"),
                    }
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
            mm["choices"] = [
                {
                    "name": c.get("name"),
                    "change": c.get("change"),
                }
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
    mode = (mode or "pre").lower()
    action = (action or "overview").lower()

    title = str(match_meta.get("title") or "Матч").strip()
    league = str(match_meta.get("league") or "").strip()
    sport = str(match_meta.get("sport") or "").strip()
    match_id = str(match_meta.get("id") or "").strip()
    status = str(match_meta.get("status") or "").strip()
    score = str(match_meta.get("score") or "").strip()

    base = [
        LLM_PROMPT_PREFIX,
        "",
        "Правила:",
        "- НЕ давай прогнозов и рекомендаций по ставкам",
        "- НЕ используй слова: ставь, бери, выгодно, лучше, проход, гарантия, 100%",
        "- В LIVE не показывай коэффициенты и числа — только направление и логику",
        "- Ответ структурированный. Без markdown. Коротко, но информативно.",
        "",
        f"Матч: {title}" + (f" ({league})" if league else ""),
        f"sport: {sport}",
        f"status: {status}",
        f"score: {score}",
        f"match_id: {match_id}",
        f"mode: {mode}",
        f"action: {action}",
        "",
        f"Текущий снапшот (JSON): {json.dumps(cur_snap, ensure_ascii=False)}",
    ]

    # prev_snap добавляем в prompt (полезно для логики движения),
    # но ВАЖНО: cache_key НЕ должен зависеть от prev_snap
    if prev_snap:
        base.append(f"Предыдущий снапшот (JSON): {json.dumps(prev_snap, ensure_ascii=False)}")

    if mode == "live":
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) по схеме:",
            '{"title":"...","context":["..."],"markets":[{"name":"...","direction":"up|down|flat|unknown","logic":"..."}],"risks":["..."],"disclaimer":"..."}',
            "",
            "Ограничения (LIVE):",
            "- Никаких рекомендаций, прогнозов и призывов к действию.",
            "- В LIVE не показывай числа/коэффициенты — только направление и логику.",
        ]

        if action == "pro":
            base += [
                "",
                "Формат LIVE PRO (обязательно):",
                "- В markets верни 4 блока (каждый отдельным объектом):",
                '  1) name="Факторы в пользу фаворита" (logic: 5–8 коротких буллетов)',
                '  2) name="Факторы против фаворита" (logic: 5–8 коротких буллетов)',
                '  3) name="Турнирный контекст" (logic: 4–7 буллетов; если данных мало — честно напиши что нужно подтвердить)',
                '  4) name="Чек-лист решения" (logic: 5–8 буллетов: что подтвердить / что ломает сценарий / триггеры отката)',
                "- direction ставь осмысленно; если данных мало — unknown.",
                "- Пиши языком трейдинга линии: импульс, откат, перекладка риска, дисбаланс, подтверждение.",
            ]
        else:
            base += [
                "",
                "Формат LIVE (обычный):",
                "- markets: 2–4 ключевых наблюдения (короче, чем PRO).",
            ]

    else:
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title":"...","summary":"...","key_factors":["..."],"line_logic":["..."],"risks":["..."],"disclaimer":"..."}',
        ]

    return "\n".join(base)


def _render_ui_json(analysis: Any, mode: str) -> str:
    if not isinstance(analysis, dict):
        title = "🟢 LIVE" if (mode or "").lower() == "live" else "📊 Обзор"
        return (
            f"{title}\n\n"
            "Сейчас нет достаточных данных для аккуратного разбора.\n"
            "Попробуй позже или нажми «🔄 Обновить LIVE».\n\n"
            "ℹ️ Аналитический материал. Не является рекомендацией."
        )

    title = str(analysis.get("title") or ("🟢 LIVE" if (mode or "").lower() == "live" else "📊 Обзор")).strip()
    lines: list[str] = [title]

    if analysis.get("summary"):
        lines += ["", str(analysis["summary"]).strip()]

    ctx = analysis.get("context") or []
    if isinstance(ctx, list) and ctx:
        lines.append("")
        for x in ctx[:8]:
            lines.append(f"• {x}")

    kf = analysis.get("key_factors") or []
    if isinstance(kf, list) and kf:
        lines.append("")
        lines.append("Факторы")
        for x in kf[:8]:
            lines.append(f"• {x}")

    ll = analysis.get("line_logic") or []
    if isinstance(ll, list) and ll:
        lines.append("")
        lines.append("Логика линии")
        for x in ll[:8]:
            lines.append(f"• {x}")

    mk = analysis.get("markets") or []
    if isinstance(mk, list) and mk:
        lines.append("")
        lines.append("Блоки")
        for item in mk[:6]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "Блок")).strip()
            direction = str(item.get("direction", "unknown")).strip()
            logic_val = item.get("logic")

            lines.append(f"— {name}: {direction}")

            # logic может быть строкой или списком буллетов
            if isinstance(logic_val, list):
                for b in logic_val[:12]:
                    s = str(b).strip()
                    if s:
                        lines.append(f"  • {s}")
            else:
                logic = str(logic_val or "").strip()
                if logic:
                    # если модель вернула многострочный текст — аккуратно распечатаем
                    for ln in logic.splitlines():
                        ln = ln.strip()
                        if ln:
                            # если уже с буллетом — не дублируем
                            if ln.startswith(("•", "-", "—")):
                                lines.append(f"  {ln}")
                            else:
                                lines.append(f"  • {ln}")

    risks = analysis.get("risks") or []
    if isinstance(risks, list) and risks:
        lines.append("")
        lines.append("Риски")
        for r in risks[:10]:
            lines.append(f"• {r}")

    disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()
    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


def _hash_cache_key(match_id: str, sport_slug: str, mode: str, action: str, cur_snap: Dict[str, Any]) -> str:
    """
    КЛЮЧЕВОЕ:
    - cache_key НЕ должен зависеть от user_id
    - cache_key НЕ должен зависеть от prev_snap (иначе 100 пользователей => 100 ключей)
    - cache_key зависит от текущего снапшота (если данные меняются — меняется hash)
    """
    payload = {
        "m": str(match_id),
        "sport": str(sport_slug or ""),
        "mode": str(mode or ""),
        "action": str(action or ""),
        "cur": cur_snap,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
        # prev_snap общий на матч
        prev_snap = _LIVE_SNAPSHOT_BY_MATCH.get(match_id)

        # refresh: обновляем глобальный снапшот и переводим в overview
        # ВАЖНО: не добавляем timestamp в cache_key — иначе кеш ломается
        if action == "refresh":
            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            action = "overview"

    prompt = _build_ui_prompt(match_meta, mode, action, prev_snap, cur_snap)

    h = _hash_cache_key(match_id, sport_slug, mode, action, cur_snap)
    cache_key = f"v12:ui:{sport_slug}:{match_id}:{mode}:{action}:{h}"

    schema = "ui_live" if mode == "live" else "ui_pre"

    # TTL: для LIVE PRO можно держать чуть дольше, чтобы не душить RPM
    if mode == "live":
        ttl_s = int(TTL_LIVE_S * 2) if action == "pro" else int(TTL_LIVE_S)
    else:
        ttl_s = int(TTL_PRE_S)

    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=cache_key,
        schema=schema,
        ttl_s=int(ttl_s),
        user_id=user_id,
    )

    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
    return _render_ui_json(analysis, mode=mode)


# -----------------------------
# Click map
# -----------------------------
def _extract_click_label(text_raw: str) -> str:
    m = re.match(r"клик\s*:\s*(.+)$", (text_raw or "").strip(), flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return (text_raw or "").strip()


def _map_button_to_ui(label: str) -> Optional[Tuple[str, str]]:
    s = (label or "").strip().lower()

    # prematch
    if "pre" == s or "pre " in s or "прематч" in s or "обзор" in s:
        return ("pre", "overview")
    if "1x2" in s or "moneyline" in s:
        return ("pre", "moneyline")
    if "тотал" in s or "total" in s:
        return ("pre", "total")
    if "фора" in s or "handicap" in s:
        return ("pre", "handicap")
    if "связк" in s:
        return ("pre", "links")

    # PRO (если написали просто "pro" — считаем LIVE PRO)
    if "pro" in s:
        if "pre" in s or "прематч" in s:
            return ("pre", "pro")
        return ("live", "pro")

    # live
    if ("обнов" in s or "refresh" in s) and ("live" in s or "лайв" in s):
        return ("live", "refresh")
    if "live" in s or "лайв" in s:
        return ("live", "overview")

    return None


# -----------------------------
# Diagnostics
# -----------------------------
def _format_env_status() -> str:
    keys = [
        "SPORT_API_BASE",
        "SPORT_API_KEY",
        "SPORT_API_KEY_HEADER",
        "SPORT_API_KEY_PREFIX",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "PUBLIC_URL",
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "LLM_CACHE_TTL_S",
        "LLM_CACHE_TTL_LIVE_S",
    ]
    lines = ["🔧 ENV status"]
    for k in keys:
        v = os.getenv(k)
        if k in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "SPORT_API_KEY"):
            lines.append(f"• {k}: {'✅ set' if (v and v.strip()) else '❌ missing'}")
        else:
            lines.append(f"• {k}: {(v or '').strip()}")
    return "\n".join(lines)


async def _llm_ping(user_id: int) -> str:
    prompt = "Верни корректный JSON по ui_pre схеме. Все ключи непустые."
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key="diag:ping:ui_pre",
        schema="ui_pre",
        ttl_s=0,
        user_id=user_id,
    )
    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
    title = ""
    if isinstance(analysis, dict):
        title = str(analysis.get("title") or "")
    return (
        "🧪 LLM ping\n"
        f"• provider: {(meta or {}).get('provider')}\n"
        f"• usedfallback: {(meta or {}).get('used_fallback')}\n"
        f"• lasterror: {(meta or {}).get('last_error')}\n"
        f"• elapsedms: {(meta or {}).get('elapsed_ms')}\n"
        f"• cache: {(meta or {}).get('cache')}\n"
        f"• sampletitle: {title}"
    )


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА
# ------------------------------------------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = (message or "").strip()
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # diag
    if norm == "version":
        return _md_safe_text(
            "✅ parsing.py version: 2026-01-16 v12 (LIVE PRO action + expanded ui_live JSON blocks + ttl tuning)"
        )
    if norm == "env":
        return _md_safe_text(_format_env_status())
    if norm == "llm ping":
        return _md_safe_text(await _llm_ping(user_id))
    if norm == "last_error":
        meta = (_LAST_LLM_META_BY_USER.get(user_id) or {})
        s = (
            "🧾 last_error\n"
            f"• provider: {meta.get('provider')}\n"
            f"• used_fallback: {meta.get('used_fallback')}\n"
            f"• last_error: {meta.get('last_error')}\n"
            f"• elapsed_ms: {meta.get('elapsed_ms')}\n"
            f"• cache: {meta.get('cache')}"
        )
        return _md_safe_text(s)

    # normalize "Клик:"
    if norm.startswith("клик:"):
        text_raw = text_raw.split(":", 1)[1].strip()
        norm = text_raw.lower().strip()

    # ui callback (inline)
    if norm.startswith("ui match"):
        parts = text_raw.split()
        if len(parts) < 5:
            return _md_safe_text("Некорректная команда UI.")
        match_id = parts[2].strip()
        mode = parts[3].strip().lower()
        action = parts[4].strip().lower()

        _ACTIVE_MATCH_BY_USER[user_id] = match_id
        reply = await _run_ui_llm(user_id=user_id, match_id=match_id, mode=mode, action=action)
        return _md_safe_text(_truncate_telegram(reply))

    # admin strategy
    if norm.startswith("админ"):
        ok, msg = _try_admin_update_strategy(user_id, text_raw)
        return _md_safe_text(msg)

    # strategy
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _md_safe_text(_format_expert_strategy_for_today())

    # --------- NAV: страна / лига ----------
    ctry = _parse_nav_country(text_raw)
    if ctry:
        idx = _build_index_for_user(user_id)
        hit = None
        for k in idx.keys():
            if k.lower() == ctry.lower():
                hit = k
                break
        country = hit or ctry
        _ACTIVE_COUNTRY_BY_USER[user_id] = country
        return _md_safe_text(_render_leagues(user_id, country))

    lg = _parse_nav_league(text_raw)
    if lg:
        country, league, page = lg
        idx = _build_index_for_user(user_id)

        c_hit = None
        for k in idx.keys():
            if k.lower() == country.lower():
                c_hit = k
                break
        country = c_hit or country

        leagues = idx.get(country) or {}
        l_hit = None
        for k in leagues.keys():
            if k.lower() == league.lower():
                l_hit = k
                break
        league = l_hit or league

        _ACTIVE_COUNTRY_BY_USER[user_id] = country
        _ACTIVE_LEAGUE_BY_USER[user_id] = league
        _ACTIVE_PAGE_BY_USER[user_id] = page
        return _md_safe_text(_render_matches_page(user_id, country, league, page))

    # matches today (API)
    if norm.startswith("матчи сегодня"):
        sport = text_raw.split("матчи сегодня", 1)[1].strip(" :\n\t")
        if not sport:
            return _md_safe_text(
                "Напиши: матчи сегодня football\n"
                "Варианты: football, ice-hockey, basketball, tennis, table-tennis, esports"
            )
        sport_slug = sport.strip().lower()
        _ACTIVE_SPORT_BY_USER[user_id] = sport_slug
        return _md_safe_text(await _format_matches_today_api(user_id, sport_slug))

    # match hub (API details)
    if norm.startswith("матч"):
        match_id = text_raw.split("матч", 1)[1].strip(" :\n\t")
        if not match_id:
            return _md_safe_text("Напиши: матч <id>")

        match_meta = await _get_match_context(user_id, match_id)
        _ACTIVE_MATCH_BY_USER[user_id] = str(match_meta.get("id") or match_id)

        return _md_safe_text(
            _truncate_telegram(
                _format_match_hub_text(
                    str(match_meta.get("id") or match_id),
                    title=str(match_meta.get("title") or f"Матч {match_id}"),
                    league=str(match_meta.get("league") or ""),
                    sport_slug=str(match_meta.get("sport") or ""),
                    status=str(match_meta.get("status") or ""),
                    start_time=str(match_meta.get("start_time") or ""),
                    score=str(match_meta.get("score") or ""),
                )
            )
        )

    # buttons (text map)
    label = _extract_click_label(text_raw)
    mapped = _map_button_to_ui(label)
    if mapped:
        active = _ACTIVE_MATCH_BY_USER.get(user_id)
        if not active:
            return _md_safe_text("Сначала выбери матч (из списка «Матчи сегодня»).")
        mode, action = mapped
        reply = await _run_ui_llm(user_id=user_id, match_id=active, mode=mode, action=action)
        return _md_safe_text(_truncate_telegram(reply))

    # profile
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _md_safe_text(_format_profile_text(bank, stats))

    # bank
    if "банк" in norm:
        if re.search(r"\d", norm):
            new_bank = _parse_bank_set(norm)
            if new_bank is not None:
                with db_session() as session:
                    user = bets_db.set_user_bank(session, user_id, new_bank)
                return _md_safe_text(f"Банк установлен: {user.bank:,.0f}".replace(",", " "))
            return _md_safe_text("Не понял сумму. Пример: мой банк 100000")
        else:
            with db_session() as session:
                bank = bets_db.get_user_bank(session, user_id)
            if bank is None:
                return _md_safe_text("У тебя пока не задан банк. Установи: мой банк 100000")
            return _md_safe_text(f"Текущий банк: {bank:,.0f}".replace(",", " "))

    help_text = (
        "Команды:\n\n"
        "• матчи сегодня football|ice-hockey|basketball|tennis|table-tennis|esports\n"
        "• страна: <название>\n"
        "• лига: <страна> | <лига> | <страница>\n"
        "• матч <id> (дальше кнопки PRE/LIVE)\n"
        "• стратегия\n"
        "• профиль\n"
        "• мой банк 100000\n\n"
        "LIVE PRO:\n"
        "• клик: pro  (или кнопка LIVE PRO в матче)\n\n"
        "Диагностика:\n"
        "• llm ping / env / version / last_error\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )
    return _md_safe_text(help_text)
