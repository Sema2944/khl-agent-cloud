from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy
from .llm_client import analyze_with_llm_cached

logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE (v6)
# ============================================================
_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_LIVE_SNAPSHOT_BY_USER_MATCH: Dict[str, Dict[str, Any]] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}

MSK = ZoneInfo("Europe/Moscow")

# ============================================================
# DEMO DATA
# ============================================================
DEMO_MATCHES = {
    "football": [
        {"id": "demofootball001", "title": "Зенит — Спартак", "league": "РПЛ"},
        {"id": "demofootball002", "title": "Динамо — Локомотив", "league": "РПЛ"},
    ],
    "hockey": [
        {"id": "demo_hockey_001", "title": "СКА — ЦСКА", "league": "КХЛ"},
        {"id": "demo_hockey_002", "title": "Ак Барс — Металлург", "league": "КХЛ"},
    ],
    "tennis": [
        {"id": "demotennis001", "title": "Игрок A — Игрок B", "league": "ATP"},
    ],
    "esports": [
        {"id": "demoesports001", "title": "Team Spirit — NAVI", "league": "CS2"},
    ],
}

DEMO_MARKETS = {
    "moneyline": {"home": 1.85, "draw": 3.9, "away": 2.1},
    "total": {"value": 2.5},
    "handicap": {"value": -1.5},
}

# ============================================================
# HELPERS
# ============================================================
def _norm_id(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (x or "").lower())

def _find_match(match_id: str) -> Optional[dict]:
    nid = _norm_id(match_id)
    for sport, arr in DEMO_MATCHES.items():
        for m in arr:
            if _norm_id(m["id"]) == nid:
                return {**m, "sport": sport}
    return None

def _snap_key(user_id: int, match_id: str) -> str:
    return f"{user_id}:{match_id}"

def _now() -> int:
    return int(time.time())

@contextmanager
def db_session() -> Session:
    gen = get_session()
    s = next(gen)
    try:
        yield s
    finally:
        gen.close()

# ============================================================
# UI PROMPT
# ============================================================
def _build_prompt(match: dict, mode: str, action: str, snap: dict) -> str:
    return (
        "Ты спортивный аналитик.\n"
        "Дай краткий аналитический разбор.\n"
        "НЕ давай советов по ставкам.\n"
        "Ответ СТРОГО JSON.\n\n"
        f"Матч: {match['title']} ({match['league']})\n"
        f"Режим: {mode}\n"
        f"Действие: {action}\n"
        f"Данные: {json.dumps(snap, ensure_ascii=False)}"
    )

def _render_ui(obj: dict, mode: str) -> str:
    lines = []
    lines.append(obj.get("title") or ("LIVE-обзор" if mode == "live" else "Обзор рынков"))

    for k in ("summary",):
        if obj.get(k):
            lines.append("")
            lines.append(obj[k])

    for sec in ("context", "key_factors", "line_logic"):
        items = obj.get(sec) or []
        if items:
            lines.append("")
            for x in items:
                lines.append(f"• {x}")

    risks = obj.get("risks") or []
    if risks:
        lines.append("")
        lines.append("Риски")
        for r in risks:
            lines.append(f"• {r}")

    lines.append("")
    lines.append(obj.get("disclaimer") or "Аналитика, не рекомендация.")
    return "\n".join(lines)

# ============================================================
# LLM RUNNER
# ============================================================
async def _run_ui_llm(user_id: int, match_id: str, mode: str, action: str) -> str:
    match = _find_match(match_id)
    if not match:
        return "Матч не найден."

    snap = DEMO_MARKETS
    prompt = _build_prompt(match, mode, action, snap)

    key = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=f"ui:v6:{key}",
        schema="ui_live" if mode == "live" else "ui_pre",
    )

    _LAST_LLM_META_BY_USER[user_id] = meta
    return _render_ui(analysis if isinstance(analysis, dict) else {}, mode)

# ============================================================
# MAIN AGENT
# ============================================================
async def run_dialog_agent(user_id: int, message: str) -> str:
    text = (message or "").strip()
    norm = text.lower()

    logger.info("agent user=%s text=%s", user_id, norm)

    # diagnostics
    if norm == "version":
        return "parsing.py v6 (stable ui + safe telegram)"
    if norm == "llm ping":
        a, meta = await analyze_with_llm_cached(
            "Верни корректный JSON с title",
            cache_key=f"ping:{_now()}",
            schema="ui_pre",
            ttl_s=0,
        )
        _LAST_LLM_META_BY_USER[user_id] = meta
        return f"LLM ping\nprovider: {meta.get('provider')}\nfallback: {meta.get('used_fallback')}\nerror: {meta.get('last_error')}"
    if norm == "last_error":
        meta = _LAST_LLM_META_BY_USER.get(user_id, {})
        return (
            "last_error\n"
            f"provider: {meta.get('provider')}\n"
            f"fallback: {meta.get('used_fallback')}\n"
            f"error: {meta.get('last_error')}"
        )

    # matches today
    if norm.startswith("матчи сегодня"):
        sport = norm.replace("матчи сегодня", "").strip() or "football"
        arr = DEMO_MATCHES.get(sport)
        if not arr:
            return "Нет матчей."
        lines = [f"Матчи сегодня — {sport}"]
        for m in arr:
            lines.append(f"• {m['title']} — id: {m['id']}")
        lines.append("")
        lines.append("Напиши: матч <id>")
        return "\n".join(lines)

    # match
    if norm.startswith("матч"):
        mid = text.split("матч", 1)[1].strip()
        m = _find_match(mid)
        if not m:
            return "Матч не найден."
        _ACTIVE_MATCH_BY_USER[user_id] = m["id"]
        return (
            f"Матч\n{m['title']} — {m['league']}\n"
            f"id: {m['id']}\n\n"
            "Доступно:\n"
            "• Обзор\n"
            "• LIVE"
        )

    # UI text buttons
    if norm in {"обзор", "обзор рынков"}:
        mid = _ACTIVE_MATCH_BY_USER.get(user_id)
        if not mid:
            return "Сначала выбери матч."
        return await _run_ui_llm(user_id, mid, "pre", "overview")

    if norm in {"live", "лайв"}:
        mid = _ACTIVE_MATCH_BY_USER.get(user_id)
        if not mid:
            return "Сначала выбери матч."
        return await _run_ui_llm(user_id, mid, "live", "overview")

    return (
        "Команды:\n"
        "• матчи сегодня football\n"
        "• матч <id>\n"
        "• обзор\n"
        "• live\n"
        "• llm ping / last_error"
    )
