from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy
from .llm_client import analyze_with_llm_cached, render_analysis_text

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
        "Пиши коротко, списками: факторы/логика/риски/чек-лист."
    )

# -----------------------------
# STATE (MVP)
# -----------------------------
# активный матч на пользователя (чтобы кнопки вида "Клик: 📊 Обзор" знали контекст)
_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}

# LIVE snapshots (на пользователя+матч)
_LIVE_SNAPSHOT_BY_USER_MATCH: Dict[str, Dict[str, Any]] = {}


def _snap_key(user_id: int, match_id: str) -> str:
    return f"{user_id}:{match_id}"


def _now_ts() -> int:
    return int(time.time())


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
# DEMO: матчи/рынки (вместо внешнего API)
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
        {"id": "demo_football_001", "title": "Зенит — Спартак", "league": "РПЛ"},
        {"id": "demo_football_002", "title": "Динамо — Локомотив", "league": "РПЛ"},
    ],
    "basketball": [
        {"id": "demo_basketball_001", "title": "ЦСКА — УНИКС", "league": "Единая Лига ВТБ"},
    ],
    "tennis": [
        {"id": "demo_tennis_001", "title": "Игрок A — Игрок B", "league": "ATP"},
    ],
    "esports": [
        {"id": "demo_esports_001", "title": "Team Spirit — NAVI", "league": "CS2"},
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -----------------------------
def _msk_today_date():
    return datetime.now(MSK).date()


def _format_profile_text(bank: Optional[float], stats: bets_db.UserStats) -> str:
    lines: list[str] = []
    lines.append("📊 Твой профиль")

    if bank is None:
        lines.append("Банк: ещё не задан")
        lines.append("Совет: задай банк командой вроде: `мой банк 100000`")
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
    nums = re.findall(r"(\d+[ \d]*)", message.replace("\u00a0", " "))
    if not nums:
        return None
    num = nums[0].replace(" ", "")
    try:
        return float(num)
    except ValueError:
        return None


def _format_week_report(bets: List[bets_db.Bet]) -> str:
    if not bets:
        return (
            "За последнюю неделю у тебя не было сохранённых ставок.\n"
            "Начни добавлять ставки, и я смогу делать отчёты по рынкам и результатам."
        )

    wins = [b for b in bets if b.result == "win"]
    loses = [b for b in bets if b.result == "lose"]
    pushes = [b for b in bets if b.result == "push"]
    non_push = wins + loses

    settled = len(non_push)
    pnl = sum(b.profit or 0.0 for b in non_push)
    total_stake = sum(b.stake or 0.0 for b in non_push)
    winrate = (len(wins) / settled * 100.0) if settled > 0 else 0.0
    roi = (pnl / total_stake * 100.0) if total_stake > 0 else 0.0

    lines: list[str] = []
    lines.append("📆 Отчёт за последние 7 дней")
    lines.append(f"Всего ставок: {len(bets)}")
    lines.append(f"Рассчитано (без возвратов): {settled}")
    lines.append(f"Возвратов: {len(pushes)}")
    lines.append(f"Winrate: {winrate:.1f}%")
    lines.append(f"ROI: {roi:.1f}%")
    lines.append(f"PnL: {pnl:+.0f}")
    lines.append(f"Объём ставок: {total_stake:.0f}")
    lines.append("")
    lines.append("Это базовый отчёт MVP. Позже будет разбор по лигам и рынкам.")
    return "\n".join(lines)


# -----------------------------
# Экспертная стратегия (по МСК) + админ-обновление
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
            "`админ стратегия: <текст>`"
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
        raw_text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return False, "Неверный формат. Пример: `админ стратегия: текст...`"

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

    return True, "✅ Стратегия обновлена и сохранена в БД (дата считается по МСК)."


# -----------------------------
# Матчи → матч → рынок → аналитика
# -----------------------------
def _find_match(match_id: str) -> Optional[dict]:
    for sport, arr in DEMO_MATCHES.items():
        for m in arr:
            if m["id"] == match_id:
                return {"sport": sport, **m}
    return None


def _format_matches_today(sport_key: str) -> str:
    sport_key = (sport_key or "").strip().lower()
    if sport_key not in DEMO_MATCHES:
        return (
            "Не понял спорт.\n"
            "Варианты: hockey, football, basketball, tennis, esports\n\n"
            "Пример: `матчи сегодня hockey`"
        )

    today = _msk_today_date().isoformat()
    title = DEMO_SPORTS.get(sport_key, sport_key)

    lines = [f"🏟 Матчи сегодня (по МСК) — {title}", f"Дата: {today}", ""]
    for m in DEMO_MATCHES[sport_key]:
        lines.append(f"• {m['title']} ({m['league']}) — id: {m['id']}")
    lines.append("")
    lines.append("Дальше: `матч <id>`.")
    return "\n".join(lines)


def _format_match_screen(match_id: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден (MVP демо)."

    lines = [
        "🏟 Матч",
        f"{m['title']} — {m['league']}",
        f"id: {m['id']}",
        "",
        "Выбери действие кнопками ниже 👇",
    ]
    return "\n".join(lines)


def _line_hash_for_cache(match_id: str, mode: str, action: str, cur_snap: Dict[str, Any], prev_snap: Optional[Dict[str, Any]]) -> str:
    payload = {
        "match_id": match_id,
        "mode": mode,
        "action": action,
        "cur": cur_snap,
        "prev": prev_snap,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# -----------------------------
# NEW UI helpers (PRE/LIVE)
# -----------------------------
def _line_snapshot_for_mode(mode: str) -> Dict[str, Any]:
    mode = (mode or "pre").lower()
    ml = DEMO_MARKETS["moneyline"]["data"]
    total = DEMO_MARKETS["total"]["data"]
    hc = DEMO_MARKETS["handicap"]["data"]

    if mode == "live":
        # в live специально НЕ тащим коэффициенты (по твоим правилам)
        return {
            "total_main": {"value": float(total["value"])},
            "handicap_main": {"team": hc["team"], "value": float(hc["value"])},
        }

    return {
        "moneyline": {"home": float(ml["home"]), "draw": float(ml["draw"]), "away": float(ml["away"])},
        "total_main": {"value": float(total["value"]), "over": float(total["over"]), "under": float(total["under"])},
        "handicap_main": {"team": hc["team"], "value": float(hc["value"]), "odds": float(hc["odds"])},
    }


def _build_ui_prompt(match_id: str, mode: str, action: str, prev_snap: Optional[Dict[str, Any]], cur_snap: Dict[str, Any]) -> str:
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
        "- Ответ короткий. Лучше списками.",
        "",
        f"Матч: {m['title']} ({m['league']})",
        f"match_id: {match_id}",
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
            "Задача:",
            "Сформируй LIVE-объяснение: 3-5 пунктов контекста и 1-3 ключевых рынка.",
            "Если есть предыдущий снапшот — объясни направление изменений (вверх/вниз/без изменений) без цифр коэффициентов.",
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "context": ["..."], "markets": [{"name":"Total|Handicap","direction":"up|down|flat|unknown","logic":"..."}], "risks": ["..."], "disclaimer":"..."}',
        ]
    else:
        base += [
            "",
            "Задача:",
            "Сформируй PREMATCH-объяснение по обзору/рынку: краткое резюме, факторы, логика линии, риски.",
            "",
            "Верни СТРОГО JSON (без markdown) с полями:",
            '{"title": "...", "summary":"...", "key_factors":["..."], "line_logic":["..."], "risks":["..."], "disclaimer":"..."}',
        ]

    return "\n".join(base)


def _render_ui_json(analysis: Any, mode: str) -> str:
    if isinstance(analysis, str):
        return analysis
    if not isinstance(analysis, dict):
        return "Не удалось получить разбор (неожиданный формат)."

    title = analysis.get("title") or ("🟢 LIVE-обзор" if (mode == "live") else "📊 Обзор рынков")
    lines: list[str] = [f"{title}"]

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
            name = item.get("name", "Market")
            direction = item.get("direction", "unknown")
            logic = item.get("logic", "")
            lines.append(f"— {name}: {direction}")
            if logic:
                lines.append(f"  {logic}")

    risks = analysis.get("risks") or []
    if risks:
        lines.append("")
        lines.append("Риски")
        for r in risks[:6]:
            lines.append(f"• {r}")

    disclaimer = analysis.get("disclaimer") or "Аналитический материал, не является рекомендацией."
    lines.append("")
    lines.append(disclaimer)
    return "\n".join(lines)


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


async def _llm_ping() -> str:
    prompt = "Верни корректный JSON по ui_pre схеме. Все ключи непустые."
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=f"diag:ping:{int(time.time())}",
        schema="ui_pre",
        ttl_s=0,  # пинг не кешируем
    )

    sample_title = ""
    if isinstance(analysis, dict):
        sample_title = str(analysis.get("title") or "")

    return (
        "🧪 LLM ping\n"
        f"• provider: {meta.get('provider')}\n"
        f"• usedfallback: {meta.get('used_fallback')}\n"
        f"• lasterror: {meta.get('last_error')}\n"
        f"• elapsedms: {meta.get('elapsed_ms')}\n"
        f"• cache: {meta.get('cache')}\n"
        f"• sampletitle: {sample_title}"
    )


# -----------------------------
# КЛИКИ ИЗ TELEGRAM (ВАЖНО)
# -----------------------------
def _extract_click_label(text_raw: str) -> Optional[str]:
    # "Клик: 📊 Обзор" / "Клик: 🟢 LIVE" / "Клик: Обновить LIVE"
    m = re.match(r"клик\s*:\s*(.+)$", (text_raw or "").strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def _map_click_to_ui(label: str) -> Optional[Tuple[str, str]]:
    """
    Возвращает (mode, action)
    """
    s = (label or "").strip().lower()

    # обзор (prematch)
    if "обзор" in s:
        return ("pre", "overview")

    # подробности (prematch)
    if "1x2" in s or "moneyline" in s:
        return ("pre", "moneyline")
    if "тотал" in s or "total" in s:
        return ("pre", "total")
    if "фора" in s or "handicap" in s:
        return ("pre", "handicap")

    # live
    if "live" in s and ("обнов" in s or "refresh" in s):
        return ("live", "refresh")
    if "live" in s:
        return ("live", "overview")

    return None


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА
# ------------------------------------------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # ---- diagnostics
    if norm == "version":
        return "✅ parsing.py version: 2025-12-27 v4 (click routing + ui pre/live + llm ping + env)"

    if norm == "env":
        return _format_env_status()

    if norm == "llm ping":
        return await _llm_ping()

    # ---- UI callback form (если у тебя где-то уже есть callback_data="ui match ...")
    # ui match <match_id> <mode> <action>
    if norm.startswith("ui match"):
        parts = text_raw.split()
        if len(parts) < 5:
            return "Некорректная команда UI."
        match_id = parts[2].strip()
        mode = parts[3].strip().lower()
        action = parts[4].strip().lower()

        m = _find_match(match_id)
        if not m:
            return "Матч не найден (MVP демо)."

        cur_snap = _line_snapshot_for_mode(mode)

        prev_snap = None
        force_refresh = False
        if mode == "live":
            k = _snap_key(user_id, match_id)
            prev_snap = (_LIVE_SNAPSHOT_BY_USER_MATCH.get(k) or {}).get("line")
            if action == "refresh":
                _LIVE_SNAPSHOT_BY_USER_MATCH[k] = {"ts": _now_ts(), "line": cur_snap}
                action = "overview"
                force_refresh = True

        prompt = _build_ui_prompt(match_id, mode, action, prev_snap, cur_snap)
        if not prompt:
            return "Не удалось собрать контекст для UI-разбора."

        h = _line_hash_for_cache(match_id, mode, action, cur_snap, prev_snap)
        suffix = f":r{_now_ts()}" if force_refresh else ""
        cache_key = f"v4:ui:{match_id}:{mode}:{action}:{h}{suffix}"

        schema = "ui_live" if mode == "live" else "ui_pre"
        analysis, meta = await analyze_with_llm_cached(prompt, cache_key=cache_key, schema=schema)

        logger.info(
            "LLM meta(ui): %s",
            {k: meta.get(k) for k in ("provider", "attempts", "elapsed_ms", "used_fallback", "last_error", "cache")},
        )
        return _render_ui_json(analysis, mode=mode)

    # ---- ТВОЯ ПРОБЛЕМА: "Клик: ..."
    click_label = _extract_click_label(text_raw)
    if click_label:
        mapped = _map_click_to_ui(click_label)
        match_id = _ACTIVE_MATCH_BY_USER.get(user_id, "")
        if not match_id:
            return "Не вижу активный матч. Сначала выбери матч командой: `матч <id>`."
        if not mapped:
            return "Не понял действие кнопки (MVP)."
        mode, action = mapped
        return await run_dialog_agent(user_id=user_id, message=f"ui match {match_id} {mode} {action}")

    # ---- 0) Админ стратегия
    if norm.startswith("админ"):
        _, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # ---- 1) Эксперт стратегия
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _format_expert_strategy_for_today()

    # ---- 2) Матчи сегодня <sport>
    if norm.startswith("матчи сегодня"):
        sport = text_raw.split("матчи сегодня", 1)[1].strip(" :\n\t")
        if not sport:
            return "Напиши: `матчи сегодня hockey` (варианты: hockey, football, basketball, tennis, esports)"
        return _format_matches_today(sport)

    if "кхл сегодня" in norm:
        return _format_matches_today("hockey")

    # ---- 3) Матч <id>
    if norm.startswith("матч"):
        match_id = text_raw.split("матч", 1)[1].strip(" :\n\t")
        if not match_id:
            return "Напиши: `матч <id>`"
        m = _find_match(match_id)
        if not m:
            return "Матч не найден (MVP демо)."
        _ACTIVE_MATCH_BY_USER[user_id] = match_id  # ✅ сохраняем активный матч
        return _format_match_screen(match_id)

    # ---- 4) Профиль
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _format_profile_text(bank, stats)

    # ---- 5) Состояние банка
    if "состояние банка" in norm or (("банк" in norm) and ("мой" in norm or "мне" in norm) and not re.search(r"\d", norm)):
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
        if bank is None:
            return "У тебя пока не задан банк. Установи: `мой банк 100000`"
        return f"Текущий банк: {bank:,.0f}".replace(",", " ")

    # ---- 6) Установка банка
    if "банк" in norm:
        new_bank = _parse_bank_set(norm)
        if new_bank is not None:
            with db_session() as session:
                user = bets_db.set_user_bank(session, user_id, new_bank)
            return f"Банк установлен: {user.bank:,.0f}".replace(",", " ")

    # ---- 7) Отчёт за неделю
    if "отчёт за неделю" in norm or "отчет за неделю" in norm:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        with db_session() as session:
            all_bets = bets_db.get_all_bets(session, user_id)
        last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
        return _format_week_report(last_week_bets)

    # ---- default help
    return (
        "Команды:\n\n"
        "• матчи сегодня hockey|football|basketball|tennis|esports\n"
        "• матч <id> (дальше кнопки PRE/LIVE)\n"
        "• стратегия\n"
        "• профиль\n\n"
        "Диагностика:\n"
        "• llm ping / env / version\n\n"
        "Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам."
    )
