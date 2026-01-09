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

_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_ACTIVE_SPORT_BY_USER: Dict[int, str] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}
_LIVE_SNAPSHOT_BY_USER_MATCH: Dict[str, Dict[str, Any]] = {}

# кеш матчей "сегодня" по пользователю: match_id -> meta
_MATCH_CACHE_BY_USER: Dict[int, Dict[str, Dict[str, Any]]] = {}

API_SPORTS_LABELS = {
    "football": "⚽ Футбол",
    "ice-hockey": "🏒 Хоккей",
    "basketball": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",
    "table-tennis": "🏓 Настольный теннис",
    "esports": "🎮 Киберспорт",
}


def _snap_key(user_id: int, match_id: str) -> str:
    return f"{user_id}:{match_id}"


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

    with db_session() as session:
        row = _get_strategy_row(session, today)
        if row and row.text:
            text = row.text
            date_label = row.date.isoformat()

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
        return (
            f"🏟 Матчи сегодня (по МСК) — {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "Не удалось получить матчи из API.\n"
            f"Причина: {str(e)[:180]}"
        )
    except Exception:
        logger.exception("Sport API error")
        return (
            f"🏟 Матчи сегодня (по МСК) — {title}\n"
            f"Дата: {today.isoformat()}\n\n"
            "Не удалось получить матчи (ошибка сервера)."
        )

    # кешируем матчи (чтобы потом по MATCH:<id> понять sport/league/title)
    _MATCH_CACHE_BY_USER[user_id] = {}
    for m in matches:
        _MATCH_CACHE_BY_USER[user_id][str(m.id)] = {
            "sport": m.sport_slug,
            "title": m.title,
            "league": m.league,
            "status": m.status,
            "start_time": m.start_time,
            "score": m.score,
            "odds_base": m.odds_base,
        }

    lines = [f"🏟 Матчи сегодня (по МСК) — {title}", f"Дата: {today.isoformat()}", ""]

    if not matches:
        lines.append("Пока нет матчей на сегодня по этому виду спорта.")
        return "\n".join(lines)

    for m in matches[:30]:
        league = f" ({m.league})" if m.league else ""
        score = f" · {m.score}" if m.score else ""
        status = f" · {_fmt_status_ru(m.status)}" if m.status else ""
        lines.append(f"• {m.title}{league}{score}{status} — id: {_md_escape(m.id)}")

    lines.append("")
    lines.append("Дальше: матч <id>.")
    return "\n".join(lines)


def _fmt_status_ru(status: str) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "—"
    map_ru = {
        "not_started": "не начался",
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
    """
    Возвращает: sport, title, league, status, start_time, score, odds_base
    1) пробуем кеш пользователя
    2) если нет — пробуем последний выбранный sport -> match_details
    3) иначе минимум
    """
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
                "sport": d.sport_slug,
                "title": d.title,
                "league": d.league,
                "status": d.status,
                "start_time": d.start_time,
                "score": d.score,
                "odds_base": d.odds_base,
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
    }


def _oddsbase_snapshot(match_meta: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """
    Берём oddsBase из match_details (если есть).
    PRE: можно числа
    LIVE: минимизируем числа (не показываем odds), оставляем названия и change
    """
    ob = match_meta.get("odds_base")
    if not isinstance(ob, dict):
        return {"odds": {"present": False}}

    markets = ob.get("markets")
    if not isinstance(markets, list):
        return {"odds": {"present": True, "markets": []}}

    if (mode or "").lower() != "live":
        # PRE — отдаём как есть, но ограничим размер
        slim: List[Dict[str, Any]] = []
        for m in markets[:8]:
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
                    for c in ch[:8]
                    if isinstance(c, dict)
                ]
            slim.append(mm)
        return {"odds": {"present": True, "markets": slim}}

    # LIVE — без odd
    slim_live: List[Dict[str, Any]] = []
    for m in markets[:8]:
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
                for c in ch[:8]
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
        "- Ответ короткий. Списками.",
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
    if prev_snap:
        base.append(f"Предыдущий снапшот (JSON): {json.dumps(prev_snap, ensure_ascii=False)}")

    if mode == "live":
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "context": ["..."], "markets": [{"name":"1X2|Total|Handicap|Odds","direction":"up|down|flat|unknown","logic":"..."}], "risks": ["..."], "disclaimer":"..."}',
        ]
    else:
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "summary":"...", "key_factors":["..."], "line_logic":["..."], "risks":["..."], "disclaimer":"..."}',
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


def _hash_cache_key(match_id: str, mode: str, action: str, cur_snap: Dict[str, Any], prev_snap: Optional[Dict[str, Any]]) -> str:
    payload = {"m": match_id, "mode": mode, "action": action, "cur": cur_snap, "prev": prev_snap}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _run_ui_llm(user_id: int, match_id: str, mode: str, action: str) -> str:
    match_meta = await _get_match_context(user_id, match_id)

    # снапшот: статус/счёт/oddsBase
    cur_snap = {
        "status": match_meta.get("status"),
        "start_time": match_meta.get("start_time"),
        "score": match_meta.get("score"),
        **_oddsbase_snapshot(match_meta, mode),
    }

    prev_snap = None
    force_refresh = False

    if (mode or "").lower() == "live":
        k = _snap_key(user_id, match_id)
        prev_snap = (_LIVE_SNAPSHOT_BY_USER_MATCH.get(k) or {}).get("snap")

        if action == "refresh":
            _LIVE_SNAPSHOT_BY_USER_MATCH[k] = {"ts": _now_ts(), "snap": cur_snap}
            action = "overview"
            force_refresh = True

    prompt = _build_ui_prompt(match_meta, mode, action, prev_snap, cur_snap)
    h = _hash_cache_key(match_id, mode, action, cur_snap, prev_snap)
    suffix = f":r{_now_ts()}" if force_refresh else ""
    cache_key = f"v9:ui:{match_id}:{mode}:{action}:{h}{suffix}"

    schema = "ui_live" if (mode or "").lower() == "live" else "ui_pre"
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=cache_key,
        schema=schema,
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
        cache_key=f"diag:ping:{int(time.time())}",
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
        return _md_safe_text("✅ parsing.py version: 2026-01-09 v9 (no ui_text imports + API matches/details + oddsBase)")
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
        return _md_safe_text(reply)

    # admin strategy
    if norm.startswith("админ"):
        _, msg = _try_admin_update_strategy(user_id, text_raw)
        return _md_safe_text(msg)

    # strategy
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _md_safe_text(_format_expert_strategy_for_today())

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

    # buttons (text map)
    label = _extract_click_label(text_raw)
    mapped = _map_button_to_ui(label)
    if mapped:
        active = _ACTIVE_MATCH_BY_USER.get(user_id)
        if not active:
            return _md_safe_text("Сначала выбери матч (из списка «Матчи сегодня»).")
        mode, action = mapped
        reply = await _run_ui_llm(user_id=user_id, match_id=active, mode=mode, action=action)
        return _md_safe_text(reply)

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
        "• матч <id> (дальше кнопки PRE/LIVE)\n"
        "• стратегия\n"
        "• профиль\n"
        "• мой банк 100000\n\n"
        "Диагностика:\n"
        "• llm ping / env / version / last_error\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )
    return _md_safe_text(help_text)
