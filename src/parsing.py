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
        "Твоя задача — объяснять движение линии и рыночную логику.\n"
        "НЕ предсказывай исход и НЕ давай советов.\n"
        "Пиши коротко, списками.\n"
        "Используй язык трейдинга/рынка, но понятно русскоговорящей аудитории."
    )

# -----------------------------
# TTL policy for LLM caching
# -----------------------------
TTL_PRE_S = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())          # 15 минут
TTL_LIVE_S = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "25").strip())     # 25 секунд
TTL_LIVE_PRO_S = int((os.getenv("LLM_CACHE_TTL_LIVE_PRO_S") or "20").strip())  # PRO live чуть чаще

# -----------------------------
# PRO gating (MVP)
# -----------------------------
PRO_ENABLED = (os.getenv("PRO_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
PRO_USER_IDS_RAW = (os.getenv("PRO_USER_IDS") or "").strip()


def _parse_int_list(csv: str) -> List[int]:
    out: List[int] = []
    for x in (csv or "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except Exception:
            continue
    return out


PRO_USER_IDS = set(_parse_int_list(PRO_USER_IDS_RAW))


def is_pro(user_id: int) -> bool:
    """
    MVP: whitelist по ENV (позже заменим на БД/подписку).
    """
    if not PRO_ENABLED:
        return False
    return int(user_id or 0) in PRO_USER_IDS


_ACTIVE_MATCH_BY_USER: Dict[int, str] = {}
_ACTIVE_SPORT_BY_USER: Dict[int, str] = {}
_LAST_LLM_META_BY_USER: Dict[int, Dict[str, Any]] = {}

# LIVE snapshot should be GLOBAL PER MATCH (not per user)
_LIVE_SNAPSHOT_BY_MATCH: Dict[str, Dict[str, Any]] = {}

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
    lines.append("Это упрощённая статистика по всем твоим действиям.")
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
            "Дисклеймер: аналитический материал, не является рекомендацией.",
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

    return (
        f"🏟 Матчи сегодня (по МСК) — {title}\n"
        f"Дата: {today.isoformat()}\n\n"
        "Открой матч командой: матч <id>\n"
        "Или используй кнопки навигации в интерфейсе."
    )


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
        "- НЕ давай прогнозов и рекомендаций (никаких 'ставь/бери/лучше/выгодно')",
        "- Формат: сжато, списками, без воды",
        "- В LIVE не показывай конкретные коэффициенты и числа (только направление/логика)",
        "- Стиль: язык рынка/трейдинга, но понятно русскоговорящей аудитории",
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

    if mode == "live" and action == "pro":
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) по схеме ui_live_pro:",
            (
                '{"title":"...",'
                '"bias":{"for_favorite":["..."],"against_favorite":["..."],"for_underdog":["..."],"against_underdog":["..."]},'
                '"signals":[{"name":"...","direction":"up|down|flat|unknown","meaning":"...","confidence":"low|mid|high"}],'
                '"trade_plan":{"scenarios":[{"if":"...","then":"...","invalidates":"..."}], "timeboxing":"..."},'
                '"risks":["..."],'
                '"disclaimer":"..."}'
            ),
            "Важно: НИГДЕ не пиши 'ставь/бери'. Это не совет, а разбор факторов и сценариев.",
        ]
        return "\n".join(base)

    if mode == "live":
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) по схеме ui_live:",
            (
                '{"title":"...",'
                '"context":["..."],'
                '"markets":[{"name":"1X2|Total|Handicap|Odds","direction":"up|down|flat|unknown","logic":"..."}],'
                '"risks":["..."],'
                '"disclaimer":"..."}'
            ),
        ]
    else:
        base += [
            "",
            "Верни СТРОГО JSON (без markdown) по схеме ui_pre:",
            (
                '{"title":"...",'
                '"summary":"...",'
                '"key_factors":["..."],'
                '"line_logic":["..."],'
                '"risks":["..."],'
                '"disclaimer":"..."}'
            ),
        ]

    return "\n".join(base)


def _render_ui_json(analysis: Any, mode: str, action: str) -> str:
    """
    Универсальный рендер:
    - ui_pre
    - ui_live
    - ui_live_pro
    """
    mode = (mode or "").lower()
    action = (action or "").lower()

    if not isinstance(analysis, dict):
        title = "🟢 LIVE" if mode == "live" else "📊 Обзор"
        return (
            f"{title}\n\n"
            "Сейчас нет достаточных данных для аккуратного разбора.\n"
            "Попробуй позже или нажми «🔄 Обновить LIVE».\n\n"
            "ℹ️ Аналитический материал. Не является рекомендацией."
        )

    # ---------- LIVE PRO ----------
    if mode == "live" and action == "pro":
        title = str(analysis.get("title") or "🟢 LIVE-анализ — PRO").strip()
        lines: List[str] = [title]

        bias = analysis.get("bias") or {}
        if isinstance(bias, dict):
            ff = bias.get("for_favorite") or []
            af = bias.get("against_favorite") or []
            fu = bias.get("for_underdog") or []
            au = bias.get("against_underdog") or []

            def _blk(name: str, arr: Any, maxn: int = 5) -> None:
                if not arr:
                    return
                lines.append("")
                lines.append(name)
                if isinstance(arr, list):
                    for x in arr[:maxn]:
                        lines.append(f"• {x}")
                else:
                    lines.append(f"• {str(arr)}")

            _blk("Факторы в пользу фаворита", ff)
            _blk("Факторы против фаворита", af)
            _blk("Факторы в пользу андердога", fu)
            _blk("Факторы против андердога", au)

        sig = analysis.get("signals") or []
        if sig:
            lines.append("")
            lines.append("Сигналы рынка")
            if isinstance(sig, list):
                for s in sig[:4]:
                    if not isinstance(s, dict):
                        continue
                    nm = str(s.get("name") or "Сигнал")
                    direction = str(s.get("direction") or "unknown")
                    meaning = str(s.get("meaning") or "").strip()
                    conf = str(s.get("confidence") or "").strip()
                    tail = f" ({conf})" if conf else ""
                    lines.append(f"— {nm}: {direction}{tail}")
                    if meaning:
                        lines.append(f"  {meaning}")

        tp = analysis.get("trade_plan") or {}
        if isinstance(tp, dict):
            scen = tp.get("scenarios") or []
            timebox = str(tp.get("timeboxing") or "").strip()

            if scen:
                lines.append("")
                lines.append("План (сценарии)")
                if isinstance(scen, list):
                    for sc in scen[:3]:
                        if not isinstance(sc, dict):
                            continue
                        _if = str(sc.get("if") or "").strip()
                        _then = str(sc.get("then") or "").strip()
                        inv = str(sc.get("invalidates") or "").strip()
                        if _if:
                            lines.append(f"• Если: {_if}")
                        if _then:
                            lines.append(f"  Тогда: {_then}")
                        if inv:
                            lines.append(f"  Отмена: {inv}")

            if timebox:
                lines.append("")
                lines.append(f"Таймбокс: {timebox}")

        risks = analysis.get("risks") or []
        if risks:
            lines.append("")
            lines.append("Риски")
            if isinstance(risks, list):
                for r in risks[:6]:
                    lines.append(f"• {r}")

        disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()
        lines.append("")
        lines.append(disclaimer)
        return "\n".join(lines)

    # ---------- PRE / LIVE базовые ----------
    title = str(analysis.get("title") or ("🟢 LIVE" if mode == "live" else "📊 Обзор")).strip()
    lines2: list[str] = [title]

    if analysis.get("summary"):
        lines2 += ["", str(analysis["summary"]).strip()]

    ctx = analysis.get("context") or []
    if ctx:
        lines2.append("")
        for x in ctx[:6]:
            lines2.append(f"• {x}")

    kf = analysis.get("key_factors") or []
    if kf:
        lines2.append("")
        lines2.append("Факторы")
        for x in kf[:6]:
            lines2.append(f"• {x}")

    ll = analysis.get("line_logic") or []
    if ll:
        lines2.append("")
        lines2.append("Логика линии")
        for x in ll[:6]:
            lines2.append(f"• {x}")

    mk = analysis.get("markets") or []
    if mk:
        lines2.append("")
        lines2.append("Ключевые рынки")
        for item in mk[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "Market"))
            direction = str(item.get("direction", "unknown"))
            logic = str(item.get("logic", "")).strip()
            lines2.append(f"— {name}: {direction}")
            if logic:
                lines2.append(f"  {logic}")

    risks = analysis.get("risks") or []
    if risks:
        lines2.append("")
        lines2.append("Риски")
        for r in risks[:6]:
            lines2.append(f"• {r}")

    disclaimer = str(analysis.get("disclaimer") or "ℹ️ Аналитический материал. Не является рекомендацией.").strip()
    lines2.append("")
    lines2.append(disclaimer)
    return "\n".join(lines2)


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


def _pro_preview_text(match_title: str = "") -> str:
    t = (match_title or "").strip()
    head = "🟢 LIVE-анализ — PRO (превью)"
    if t:
        head = f"{head}\n{t}"
    return "\n".join(
        [
            head,
            "",
            "В PRO ты получаешь:",
            "• Факторы за/против фаворита и андердога (чтобы решать самому)",
            "• Сигналы рынка: что двигает линию и почему",
            "• 2–3 сценария + что является “отменой” сценария",
            "",
            "⭐ Нажми «Оформить PRO» (скоро подключим оплату).",
            "",
            "ℹ️ Аналитический материал. Не является рекомендацией.",
        ]
    )


async def _run_ui_llm(user_id: int, match_id: str, mode: str, action: str) -> str:
    match_meta = await _get_match_context(user_id, match_id)

    sport_slug = str(match_meta.get("sport") or "").strip().lower()
    match_id = str(match_meta.get("id") or match_id).strip()
    mode = (mode or "pre").strip().lower()
    action = (action or "overview").strip().lower()

    # PRO gating: только LIVE PRO
    if mode == "live" and action == "pro" and not is_pro(user_id):
        return _pro_preview_text(str(match_meta.get("title") or ""))

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
            _LIVE_SNAPSHOT_BY_MATCH[match_id] = cur_snap
            action = "overview"

    prompt = _build_ui_prompt(match_meta, mode, action, prev_snap, cur_snap)

    h = _hash_cache_key(match_id, sport_slug, mode, action, cur_snap)
    cache_key = f"v12:ui:{sport_slug}:{match_id}:{mode}:{action}:{h}"

    if mode == "live" and action == "pro":
        schema = "ui_live_pro"
        ttl_s = TTL_LIVE_PRO_S
    else:
        schema = "ui_live" if mode == "live" else "ui_pre"
        ttl_s = TTL_LIVE_S if mode == "live" else TTL_PRE_S

    analysis, meta = await analyze_with_llm_cached(
        prompt,
        cache_key=cache_key,
        schema=schema,
        ttl_s=int(ttl_s),
        user_id=user_id,
    )

    _LAST_LLM_META_BY_USER[user_id] = dict(meta or {})
    return _render_ui_json(analysis, mode=mode, action=action)


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

    # live
    if ("обнов" in s or "refresh" in s) and ("live" in s or "лайв" in s):
        return ("live", "refresh")
    if "pro" in s and ("live" in s or "лайв" in s):
        return ("live", "pro")
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
        "LLM_CACHE_TTL_LIVE_PRO_S",
        "PRO_ENABLED",
        "PRO_USER_IDS",
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
        return _md_safe_text("✅ parsing.py version: 2026-01-18 v12 (LIVE PRO action + PRO gating MVP + TTL live_pro)")
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

    # premium text fallback (если нажали текстом)
    if "premium" in norm or "премиум" in norm:
        pro_line = "✅ PRO активен" if is_pro(user_id) else "🔒 PRO не активен"
        return _md_safe_text(
            "\n".join(
                [
                    "⭐ Premium / PRO",
                    pro_line,
                    "",
                    "PRO даёт:",
                    "• LIVE PRO: факторы за/против фаворита и андердога",
                    "• Сигналы движения линии + сценарии и отмены",
                    "",
                    "Пока оплата не подключена.",
                ]
            )
        )

    help_text = (
        "Команды:\n\n"
        "• матчи сегодня football|ice-hockey|basketball|tennis|table-tennis|esports\n"
        "• матч <id> (дальше кнопки PRE/LIVE/LIVE PRO)\n"
        "• стратегия\n"
        "• профиль\n"
        "• мой банк 100000\n\n"
        "Диагностика:\n"
        "• llm ping / env / version / last_error\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )
    return _md_safe_text(help_text)
