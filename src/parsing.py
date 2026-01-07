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
from typing import Optional, Tuple, Dict, Any

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy
from .llm_client import analyze_with_llm_cached

# ✅ NEW: эталонные тексты UI (без "AI недоступен")
from .ui_text import (
    MatchCard,
    LiveState,
    text_match,
    text_pre_overview,
    text_pre_1x2,
    text_pre_total,
    text_pre_handicap,
    text_pre_links,
    text_live_overview,
    text_live_full,
)

logger = logging.getLogger(__name__)

# -----------------------------
# Настройки
# -----------------------------
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD (fallback)

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
# STATE (MVP)
# -----------------------------
_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_LIVE_SNAPSHOT_BY_USER_MATCH: Dict[str, Dict[str, Any]] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}  # хранит meta для last_error


def _snap_key(user_id: int, match_id: str) -> str:
    return f"{user_id}:{match_id}"


def _now_ts() -> int:
    return int(time.time())


def _msk_today_date():
    return datetime.now(MSK).date()


def _norm_id(x: str) -> str:
    """demo_football_001 == demofootball001 == DEMO-FOOTBALL-001"""
    return re.sub(r"[^a-z0-9]+", "", (x or "").lower())


def _md_escape(s: str) -> str:
    """
    telegram.ext часто шлёт parse_mode="Markdown".
    Чтобы не ловить BadRequest: Can't parse entities — экранируем спец-символы.
    """
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
    # Самый надёжный вариант — экранировать всё сообщение целиком.
    return _md_escape(text or "")


# -----------------------------
# УТИЛИТА ДЛЯ SESSION (вне FastAPI)
# -----------------------------
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
# DEMO: матчи/рынки
# -----------------------------
DEMO_SPORTS = {
    "hockey": "🏒 Хоккей",
    "football": "⚽ Футбол",
    "basketball": "🏀 Баскетбол",
    "tennis": "🎾 Теннис",
    "esports": "🎮 Киберспорт",
}

DEMO_MATCHES = {
    "hockey": [
        {"id": "demo_hockey_001", "title": "СКА — ЦСКА", "league": "КХЛ"},
        {"id": "demo_hockey_002", "title": "Ак Барс — Металлург", "league": "КХЛ"},
    ],
    "football": [
        {"id": "demofootball001", "title": "Зенит — Спартак", "league": "РПЛ"},
        {"id": "demofootball002", "title": "Динамо — Локомотив", "league": "РПЛ"},
    ],
    "basketball": [
        {"id": "demobasketball001", "title": "ЦСКА — УНИКС", "league": "Единая Лига ВТБ"},
    ],
    "tennis": [
        {"id": "demotennis001", "title": "Игрок A — Игрок B", "league": "ATP"},
    ],
    "esports": [
        {"id": "demoesports001", "title": "Team Spirit — NAVI", "league": "CS2"},
    ],
}

DEMO_MARKETS = {
    "moneyline": {
        "label": "1X2 / Moneyline",
        "data": {"type": "moneyline", "home": 1.85, "draw": 3.90, "away": 2.10},
    },
    "total": {
        "label": "Тотал (Over/Under)",
        "data": {"type": "total", "value": 5.5, "over": 1.87, "under": 1.95},
    },
    "handicap": {
        "label": "Фора (Handicap)",
        "data": {"type": "handicap", "team": "home", "value": -1.5, "odds": 2.35},
    },
}


# -----------------------------
# UI helpers (card)
# -----------------------------
def _split_title(title: str) -> tuple[str, str]:
    t = (title or "").strip()
    if "—" in t:
        a, b = t.split("—", 1)
        return a.strip(), b.strip()
    if "-" in t:
        a, b = t.split("-", 1)
        return a.strip(), b.strip()
    return t, ""


def _card_from_match(m: dict) -> MatchCard:
    home, away = _split_title(m.get("title", ""))
    return MatchCard(
        match_id=str(m.get("id", "")),
        home=home or "Команда 1",
        away=away or "Команда 2",
        league=str(m.get("league", "")),
        # date_str/time_str пока не храним в DEMO_MATCHES — можно добавить позже
        date_str=None,
        time_str=None,
    )


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
# Матчи / матч
# -----------------------------
def _find_match(match_id: str) -> Optional[dict]:
    target = _norm_id(match_id)
    for sport, arr in DEMO_MATCHES.items():
        for m in arr:
            if _norm_id(m.get("id", "")) == target:
                return {"sport": sport, **m}
    return None


def _normalize_match_id(match_id: str) -> Optional[str]:
    """
    Если пользователь прислал "demo_hockey_001" или "DEMO-HOCKEY-001" — возвращаем реальный id из DEMO_MATCHES.
    """
    target = _norm_id(match_id)
    for sport, arr in DEMO_MATCHES.items():
        for m in arr:
            if _norm_id(m.get("id", "")) == target:
                return str(m["id"])
    return None


def _format_matches_today(sport_key: str) -> str:
    sport_key = (sport_key or "").strip().lower()
    if sport_key not in DEMO_MATCHES:
        return (
            "Не понял спорт.\n"
            "Варианты: hockey, football, basketball, tennis, esports\n"
            "Пример: матчи сегодня football"
        )

    today = _msk_today_date().isoformat()
    title = DEMO_SPORTS.get(sport_key, sport_key)

    lines = [f"🏟 Матчи сегодня (по МСК) — {title}", f"Дата: {today}", ""]
    for m in DEMO_MATCHES[sport_key]:
        lines.append(f"• {m['title']} ({m['league']}) — id: {_md_escape(m['id'])}")
    lines.append("")
    lines.append("Дальше: матч <id>.")
    return "\n".join(lines)


def _format_match_screen(match_id: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден (MVP демо)."
    card = _card_from_match(m)
    return text_match(card)


def _format_market(match_id: str, market_key: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден."
    mk = (market_key or "").strip().lower()
    if mk not in DEMO_MARKETS:
        return "Не понял рынок. Варианты: moneyline, total, handicap"

    info = DEMO_MARKETS[mk]
    data = info["data"]
    lines = [f"📌 Рынок: {info['label']}", f"Матч: {m['title']} ({m['league']})", ""]
    if mk == "moneyline":
        lines += [
            f"П1: {data['home']}",
            f"X: {data['draw']}",
            f"П2: {data['away']}",
        ]
    elif mk == "total":
        lines += [
            f"Тотал: {data['value']}",
            f"Больше: {data['over']}",
            f"Меньше: {data['under']}",
        ]
    else:
        lines += [
            f"Команда: {data['team']}",
            f"Фора: {data['value']}",
            f"Кф: {data['odds']}",
        ]
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)


# -----------------------------
# UI / LLM
# -----------------------------
def _line_snapshot_for_mode(mode: str) -> Dict[str, Any]:
    mode = (mode or "pre").lower()
    ml = DEMO_MARKETS["moneyline"]["data"]
    total = DEMO_MARKETS["total"]["data"]
    hc = DEMO_MARKETS["handicap"]["data"]

    if mode == "live":
        # в live держим минимум чисел — правила prompt запрещают числа в ответе
        return {
            "total_main": {"value": float(total["value"])},
            "handicap_main": {"team": hc["team"], "value": float(hc["value"])},
        }

    return {
        "moneyline": {"home": float(ml["home"]), "draw": float(ml["draw"]), "away": float(ml["away"])},
        "total_main": {"value": float(total["value"]), "over": float(total["over"]), "under": float(total["under"])},
        "handicap_main": {"team": hc["team"], "value": float(hc["value"]), "odds": float(hc["odds"])},
    }


def _build_ui_prompt(
    match_id: str,
    mode: str,
    action: str,
    prev_snap: Optional[Dict[str, Any]],
    cur_snap: Dict[str, Any],
) -> str:
    m = _find_match(match_id)
    if not m:
        return ""

    mode = (mode or "pre").lower()
    action = (action or "overview").lower()

    base = [
        LLM_PROMPT_PREFIX,
        "",
        "Правила:",
        "- НЕ давай прогнозов и рекомендаций по ставкам",
        "- НЕ используй слова: ставь, бери, выгодно, лучше, проход, гарантия, 100%",
        "- В LIVE не показывай коэффициенты и числа — только направление и логику",
        "- Ответ короткий. Списками.",
        "",
        f"Матч: {m['title']} ({m['league']})",
        f"match_id: {m['id']}",
        f"mode: {mode}",
        f"action: {action}",
        "",
        f"Текущий снапшот линии (JSON): {json.dumps(cur_snap, ensure_ascii=False)}",
    ]
    if prev_snap:
        base.append(f"Предыдущий снапшот линии (JSON): {json.dumps(prev_snap, ensure_ascii=False)}")

    if mode == "live":
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "context": ["..."], "markets": [{"name":"Total|Handicap","direction":"up|down|flat|unknown","logic":"..."}], "risks": ["..."], "disclaimer":"..."}',
        ]
    else:
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "summary":"...", "key_factors":["..."], "line_logic":["..."], "risks":["..."], "disclaimer":"..."}',
        ]

    return "\n".join(base)


def _render_ui_json(
    analysis: Any,
    *,
    mode: str,
    action: str,
    card: MatchCard,
    used_fallback: bool,
) -> str:
    """
    ✅ Главное: НИКАКИХ "AI недоступен".
    Если analysis не dict или used_fallback=True → отдаём красивые эталонные тексты из ui_text.py
    """
    mode_l = (mode or "pre").lower()
    action_l = (action or "overview").lower()

    if used_fallback or not isinstance(analysis, dict):
        if mode_l == "live":
            live = LiveState(live_time="—", score="—")
            if action_l in {"full", "deep"}:
                return text_live_full(card, live, fallback=True)
            return text_live_overview(card, live, fallback=True)

        # pre
        if action_l == "moneyline":
            return text_pre_1x2(card, fallback=True)
        if action_l == "total":
            return text_pre_total(card, fallback=True)
        if action_l == "handicap":
            return text_pre_handicap(card, fallback=True)
        if action_l in {"links", "bundle", "bundles"}:
            return text_pre_links(card)
        return text_pre_overview(card, fallback=True)

    # ---- если LLM вернул нормальный JSON, рендерим компактно (как раньше), но без токсичных фраз ----
    title = str(analysis.get("title") or ("🟢 LIVE-обзор" if mode_l == "live" else "📊 Обзор рынков")).strip()
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
        for item in mk[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "Market"))
            direction = str(item.get("direction", "unknown"))
            logic = str(item.get("logic", ""))
            lines.append(f"— {name}: {direction}")
            if logic:
                lines.append(f"  {logic}")

    risks = analysis.get("risks") or []
    if risks:
        lines.append("")
        lines.append("Риски")
        for r in risks[:6]:
            lines.append(f"• {r}")

    # единый дисклеймер внизу
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)


def _hash_cache_key(match_id: str, mode: str, action: str, cur_snap: Dict[str, Any], prev_snap: Optional[Dict[str, Any]]) -> str:
    payload = {"m": match_id, "mode": mode, "action": action, "cur": cur_snap, "prev": prev_snap}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _run_ui_llm(user_id: int, match_id: str, mode: str, action: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден (MVP демо)."

    card = _card_from_match(m)
    cur_snap = _line_snapshot_for_mode(mode)

    prev_snap = None
    force_refresh = False
    if (mode or "").lower() == "live":
        k = _snap_key(user_id, m["id"])
        prev_snap = (_LIVE_SNAPSHOT_BY_USER_MATCH.get(k) or {}).get("line")
        if action == "refresh":
            _LIVE_SNAPSHOT_BY_USER_MATCH[k] = {"ts": _now_ts(), "line": cur_snap}
            action = "overview"
            force_refresh = True

    prompt = _build_ui_prompt(m["id"], mode, action, prev_snap, cur_snap)
    if not prompt:
        return "Не удалось собрать контекст для UI-разбора."

    h = _hash_cache_key(m["id"], mode, action, cur_snap, prev_snap)
    suffix = f":r{_now_ts()}" if force_refresh else ""
    cache_key = f"v7:ui:{m['id']}:{mode}:{action}:{h}{suffix}"

    schema = "ui_live" if (mode or "").lower() == "live" else "ui_pre"
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=cache_key,
        schema=schema,
        user_id=user_id,
    )

    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})

    logger.info(
        "LLM meta(ui): %s",
        {k: (meta or {}).get(k) for k in ("provider", "elapsed_ms", "used_fallback", "last_error", "cache")},
    )

    used_fallback = bool((meta or {}).get("used_fallback"))
    return _render_ui_json(
        analysis,
        mode=mode,
        action=action,
        card=card,
        used_fallback=used_fallback,
    )


# -----------------------------
# КНОПКИ (входящие тексты)
# -----------------------------
def _extract_click_label(text_raw: str) -> str:
    """
    Иногда Telegram присылает "Клик: 📊 Обзор"
    Иногда кнопка просто отправляет свой текст.
    """
    m = re.match(r"клик\s*:\s*(.+)$", (text_raw or "").strip(), flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return (text_raw or "").strip()


def _map_button_to_ui(label: str) -> Optional[Tuple[str, str]]:
    s = (label or "").strip().lower()

    # prematch обзор
    if "обзор" in s:
        return ("pre", "overview")

    # prematch подробности
    if "1x2" in s or "moneyline" in s:
        return ("pre", "moneyline")
    if "тотал" in s or "total" in s:
        return ("pre", "total")
    if "фора" in s or "handicap" in s:
        return ("pre", "handicap")

    # ✅ связки
    if "связк" in s or "links" in s or "bundle" in s:
        return ("pre", "links")

    # live
    if ("обнов" in s and "live" in s) or ("обнов" in s and "лайв" in s):
        return ("live", "refresh")
    if "live" in s or "лайв" in s:
        # если ты добавишь кнопку "LIVE полный" — просто мапни на ("live","full")
        if "полный" in s or "full" in s or "deep" in s:
            return ("live", "full")
        return ("live", "overview")

    return None


# -----------------------------
# Diagnostics
# -----------------------------
def _format_env_status() -> str:
    keys = [
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "PUBLIC_URL",
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "LLM_TOTAL_TIMEOUT_S",
        "LLM_ATTEMPT_TIMEOUT_S",
        "LLM_MAX_RETRIES",
    ]
    lines = ["🔧 ENV status"]
    for k in keys:
        v = os.getenv(k)
        if k in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"):
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

    # -----------------------------
    # Diagnostics
    # -----------------------------
    if norm == "version":
        return _md_safe_text("✅ parsing.py version: 2026-01-07 v7 (ui_text templates + no 'AI недоступен')")
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

    # -----------------------------
    # Normalize "Клик: ..."
    # -----------------------------
    if norm.startswith("клик:"):
        text_raw = text_raw.split(":", 1)[1].strip()
        norm = text_raw.lower().strip()

    # -----------------------------
    # UI callback_data support (InlineKeyboard)
    # формат: ui match <match_id> <pre|live> <action>
    # -----------------------------
    if norm.startswith("ui match"):
        parts = text_raw.split()
        if len(parts) < 5:
            return _md_safe_text("Некорректная команда UI.")
        match_id = parts[2].strip()
        mode = parts[3].strip().lower()
        action = parts[4].strip().lower()

        m = _find_match(match_id)
        if not m:
            fixed = _normalize_match_id(match_id)
            m = _find_match(fixed) if fixed else None
        if not m:
            return _md_safe_text("Матч не найден (MVP демо).")

        _ACTIVE_MATCH_BY_USER[user_id] = m["id"]
        reply = await _run_ui_llm(user_id=user_id, match_id=m["id"], mode=mode, action=action)
        return _md_safe_text(reply)

    # -----------------------------
    # Admin strategy
    # -----------------------------
    if norm.startswith("админ"):
        _, msg = _try_admin_update_strategy(user_id, text_raw)
        return _md_safe_text(msg)

    # -----------------------------
    # Strategy
    # -----------------------------
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _md_safe_text(_format_expert_strategy_for_today())

    # -----------------------------
    # Matches today
    # -----------------------------
    if norm.startswith("матчи сегодня"):
        sport = text_raw.split("матчи сегодня", 1)[1].strip(" :\n\t")
        if not sport:
            return _md_safe_text("Напиши: матчи сегодня football (варианты: hockey, football, basketball, tennis, esports)")
        return _md_safe_text(_format_matches_today(sport))

    if "кхл сегодня" in norm:
        return _md_safe_text(_format_matches_today("hockey"))

    # -----------------------------
    # Match <id>
    # -----------------------------
    if norm.startswith("матч"):
        match_id = text_raw.split("матч", 1)[1].strip(" :\n\t")
        if not match_id:
            return _md_safe_text("Напиши: матч <id>")

        m = _find_match(match_id)
        if not m:
            fixed = _normalize_match_id(match_id)
            m = _find_match(fixed) if fixed else None
        if not m:
            return _md_safe_text("Матч не найден (MVP демо).")

        _ACTIVE_MATCH_BY_USER[user_id] = m["id"]
        return _md_safe_text(_format_match_screen(m["id"]))

    # -----------------------------
    # Buttons: text click-map
    # -----------------------------
    label = _extract_click_label(text_raw)
    mapped = _map_button_to_ui(label)
    if mapped:
        active = _ACTIVE_MATCH_BY_USER.get(user_id)
        if not active:
            return _md_safe_text("Сначала выбери матч: матч <id>")
        mode, action = mapped
        reply = await _run_ui_llm(user_id=user_id, match_id=active, mode=mode, action=action)
        return _md_safe_text(reply)

    # -----------------------------
    # Profile
    # -----------------------------
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _md_safe_text(_format_profile_text(bank, stats))

    # -----------------------------
    # Bank set/show
    # -----------------------------
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

    # -----------------------------
    # Legacy: рынок <id> <market_key>
    # -----------------------------
    if norm.startswith("рынок"):
        body = text_raw.split("рынок", 1)[1].strip()
        parts = body.split()
        if len(parts) < 2:
            return _md_safe_text("Напиши: рынок <match_id> <market_key>")
        match_id, market_key = parts[0], parts[1]
        fixed = _normalize_match_id(match_id)
        if fixed:
            match_id = fixed
        return _md_safe_text(_format_market(match_id, market_key))

    # -----------------------------
    # Default help
    # -----------------------------
    help_text = (
        "Команды:\n\n"
        "• матчи сегодня hockey|football|basketball|tennis|esports\n"
        "• матч <id> (дальше кнопки PRE/LIVE)\n"
        "• стратегия\n"
        "• профиль\n"
        "• мой банк 100000\n\n"
        "Диагностика:\n"
        "• llm ping / env / version / last_error\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )
    return _md_safe_text(help_text)
