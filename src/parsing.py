# src/parsing.py
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy  # ✅ модель только тут, не дублируем
from .llm_client import analyze_with_llm_cached, render_analysis_text  # ✅ cache wrapper

logger = logging.getLogger(__name__)

# -----------------------------
# Настройки
# -----------------------------
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

# Стратегия из ENV (fallback)
EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD (fallback)

# Важно: "на сегодня" считаем по МСК
MSK = ZoneInfo("Europe/Moscow")

# ✅ Промт аналитика ставок LLM (можно менять без деплоя)
LLM_PROMPT_PREFIX = (os.getenv("LLM_PROMPT_PREFIX") or "").strip()
if not LLM_PROMPT_PREFIX:
    LLM_PROMPT_PREFIX = (
        "Ты дружелюбный спортивный аналитик.\n"
        "Твоя задача — объяснять коэффициенты и логику линии.\n"
        "НЕ предсказывай исход и НЕ давай советов по ставкам.\n"
        "Пиши коротко, структурно: мысли/риски/чек-лист."
    )

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

# match_id должен быть стабильным (чтобы бот узнавал)
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

# MVP: одинаковые рынки для всех матчей (демо)
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
    lines.append("📊 *Твой профиль*")

    if bank is None:
        lines.append("Банк: _ещё не задан_")
        lines.append("Совет: задай банк командой вроде: `мой банк 100000`")
    else:
        lines.append(f"Банк: *{bank:,.0f}*".replace(",", " "))

    lines.append("")
    lines.append(f"Всего ставок: *{stats.total_bets}*")
    lines.append(f"Рассчитано ставок (без возвратов): *{stats.settled_bets}*")
    lines.append(f"Возвратов: *{stats.pushes}*")
    lines.append(f"Winrate: *{stats.winrate:.1f}%*")
    lines.append(f"ROI: *{stats.roi:.1f}%*")
    lines.append(f"PnL: *{stats.pnl:+.0f}*")
    lines.append(f"Объём ставок: *{stats.total_stake:.0f}*")
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
    lines.append("📆 *Отчёт за последние 7 дней*")
    lines.append(f"Всего ставок: *{len(bets)}*")
    lines.append(f"Рассчитано (без возвратов): *{settled}*")
    lines.append(f"Возвратов: *{len(pushes)}*")
    lines.append(f"Winrate: *{winrate:.1f}%*")
    lines.append(f"ROI: *{roi:.1f}%*")
    lines.append(f"PnL: *{pnl:+.0f}*")
    lines.append(f"Объём ставок: *{total_stake:.0f}*")
    lines.append("")
    lines.append("_Это базовый отчёт MVP. Позже будет разбор по лигам и рынкам._")
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

    # fallback на ENV, если в БД пусто
    if not text and EXPERT_STRATEGY_TEXT:
        text = EXPERT_STRATEGY_TEXT
        date_label = EXPERT_STRATEGY_DATE or date_label

    if not text:
        return (
            "👤 *Стратегия эксперта на сегодня* (по МСК)\n"
            "_Пока не опубликована._\n\n"
            "Если ты админ — обнови командой:\n"
            "`админ стратегия: <текст>`"
        )

    return "\n".join(
        [
            "👤 *Стратегия эксперта на сегодня* (по МСК)",
            f"Дата: *{date_label}*",
            "",
            text,
            "",
            "_Дисклеймер: это аналитическая заметка, не призыв к ставке. Решение всегда на стороне пользователя._",
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
            "Варианты (MVP): hockey, football, basketball, tennis, esports\n\n"
            "Пример: `матчи сегодня hockey`"
        )

    today = _msk_today_date().isoformat()
    title = DEMO_SPORTS.get(sport_key, sport_key)

    lines = [f"🏟 *Матчи сегодня* (по МСК) — {title}", f"Дата: *{today}*", ""]
    for m in DEMO_MATCHES[sport_key]:
        lines.append(f"• {m['title']} ({m['league']}) — id: `{m['id']}`")
    lines.append("")
    lines.append("_Дальше: `матч <id>`._")
    return "\n".join(lines)


def _format_match_screen(match_id: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден (MVP демо)."

    lines = [
        "🏟 *Матч*",
        f"{m['title']} — {m['league']}",
        f"id: `{m['id']}`",
        "",
        "*Выбери рынок:*",
    ]
    for key, v in DEMO_MARKETS.items():
        lines.append(f"• {v['label']} — key: `{key}`")
    lines.append("")
    lines.append("_Дальше: `рынок <id> <market_key>` → `аналитика <id> <market_key>`._")
    return "\n".join(lines)


def _format_market(match_id: str, market_key: str) -> str:
    m = _find_match(match_id)
    if not m:
        return "Матч не найден (MVP демо)."

    market_key = (market_key or "").strip().lower()
    market = DEMO_MARKETS.get(market_key)
    if not market:
        return "Рынок не найден (MVP демо)."

    data = market["data"]
    lines: list[str] = []
    lines.append("📈 *Рынок (нормализовано / MVP)*")
    lines.append(f"Матч: {m['title']} ({m['league']})")
    lines.append(f"match_id: `{match_id}`")
    lines.append(f"market: `{market_key}`")
    lines.append("")

    if data["type"] == "moneyline":
        lines.append(f"*{market['label']}*")
        lines.append(f"• Home: *{data['home']:.2f}*")
        lines.append(f"• Draw: *{data['draw']:.2f}*")
        lines.append(f"• Away: *{data['away']:.2f}*")
    elif data["type"] == "total":
        lines.append(f"*{market['label']}*")
        lines.append(f"• Over {data['value']}: *{data['over']:.2f}*")
        lines.append(f"• Under {data['value']}: *{data['under']:.2f}*")
    elif data["type"] == "handicap":
        lines.append(f"*{market['label']}*")
        lines.append(f"• Team {data['team']} {data['value']:+.1f}: *{data['odds']:.2f}*")

    lines.append("")
    lines.append("Дальше:")
    lines.append(f"• `аналитика {match_id} {market_key}` — 🧠 AI разбор")
    lines.append("• `стратегия` — 👤 стратегия дня (по МСК)")
    lines.append("")
    lines.append("_Дисклеймер: показано для объяснения рынков. Не является рекомендацией._")
    return "\n".join(lines)


def _line_hash_for_cache(match_id: str, market_key: str) -> str:
    """
    ✅ line_hash меняется, когда меняются цифры линии.
    """
    m = _find_match(match_id) or {}
    market = DEMO_MARKETS.get(market_key) or {}

    payload = {
        "match_id": match_id,
        "market_key": market_key,
        "title": m.get("title", ""),
        "league": m.get("league", ""),
        "line": market.get("data", {}),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _build_llm_domain_prompt(match_id: str, market_key: str) -> str:
    m = _find_match(match_id)
    market = DEMO_MARKETS.get(market_key)
    if not m or not market:
        return ""

    data = market["data"]
    lines: list[str] = []
    lines.append(LLM_PROMPT_PREFIX)
    lines.append("")
    lines.append(f"Матч: {m['title']} ({m['league']})")
    lines.append(f"match_id: {match_id}")
    lines.append(f"Рынок: {market['label']} ({market_key})")
    lines.append("")

    if data.get("type") == "moneyline":
        lines.append(f"Линия 1X2: home={data['home']}, draw={data['draw']}, away={data['away']}")
    elif data.get("type") == "total":
        lines.append(f"Тотал: {data['value']}, over={data['over']}, under={data['under']}")
    elif data.get("type") == "handicap":
        lines.append(f"Фора: team={data['team']}, handicap={data['value']}, odds={data['odds']}")

    lines.append("")
    lines.append("Нужно: 3–5 тезисов, 2–4 риска, 2–4 пункта чек-листа.")
    return "\n".join(lines)


async def ai_analyze(*, user_id: int, match_id: str, market_key: str) -> str:
    """
    ✅ AI аналитика через LLM + In-memory cache (через analyze_with_llm_cached).
    cache_key = v1:<match_id>:<market_key>:<line_hash>
    """
    _ = user_id  # пока не используем, но оставляем сигнатуру

    m = _find_match(match_id)
    market_key = (market_key or "").strip().lower()
    market = DEMO_MARKETS.get(market_key)

    if not m:
        return "Матч не найден (MVP демо)."
    if not market:
        return "Рынок не найден (MVP демо)."

    domain_prompt = _build_llm_domain_prompt(match_id, market_key)
    if not domain_prompt:
        return "Не удалось собрать контекст для аналитики."

    line_hash = _line_hash_for_cache(match_id, market_key)
    cache_key = f"v1:{match_id}:{market_key}:{line_hash}"

    analysis, meta = await analyze_with_llm_cached(domain_prompt, cache_key=cache_key)

    logger.info(
        "LLM meta: %s",
        {
            "provider": meta.get("provider"),
            "attempts": meta.get("attempts"),
            "elapsed_ms": meta.get("elapsed_ms"),
            "used_fallback": meta.get("used_fallback"),
            "last_error": meta.get("last_error"),
            "cache": meta.get("cache"),  # "hit" / "miss"
        },
    )

    return render_analysis_text(analysis)


def expert_opinion_for_market(match_id: str, market_key: str) -> str:
    base = _format_expert_strategy_for_today()
    return base + "\n\n" + "_Примечание: в MVP мнение эксперта публикуется как общая стратегия дня._"


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА
# ------------------------------------------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # 0) Админ стратегия
    if norm.startswith("админ"):
        _, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # 1) Эксперт стратегия (по МСК)
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _format_expert_strategy_for_today()

    # 2) Матчи сегодня <sport>
    if norm.startswith("матчи сегодня"):
        sport = text_raw.split("матчи сегодня", 1)[1].strip(" :\n\t")
        if not sport:
            return (
                "Напиши: `матчи сегодня hockey`\n"
                "Варианты: hockey, football, basketball, tennis, esports"
            )
        return _format_matches_today(sport)

    # Backward compat: "кхл сегодня" -> хоккей
    if "кхл сегодня" in norm:
        return _format_matches_today("hockey")

    # 3) Матч <match_id>
    if norm.startswith("матч"):
        match_id = text_raw.split("матч", 1)[1].strip(" :\n\t")
        if not match_id:
            return "Напиши: `матч <id>`"
        return _format_match_screen(match_id)

    # 4) Рынок <match_id> <market_key>
    if norm.startswith("рынок"):
        body = text_raw.split("рынок", 1)[1].strip()
        parts = body.split()
        if len(parts) < 2:
            return "Напиши: `рынок <match_id> <market_key>`"
        match_id, market_key = parts[0], parts[1]
        return _format_market(match_id, market_key)

    # 5) Аналитика <match_id> <market_key>
    if norm.startswith("аналитика"):
        body = text_raw.split("аналитика", 1)[1].strip()
        parts = body.split()
        if len(parts) >= 2:
            return await ai_analyze(user_id=user_id, match_id=parts[0], market_key=parts[1])
        return (
            "Напиши:\n"
            "• `аналитика <match_id> <market_key>`\n"
            "или по шагам: `матчи сегодня hockey` → `матч <id>` → `рынок <id> <market>`"
        )

    # 6) Эксперт мнение (кнопка/команда)
    if norm.startswith("эксперт") or norm.startswith("мнение эксперта"):
        return expert_opinion_for_market("", "")

    # 7) Профиль
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _format_profile_text(bank, stats)

    # 8) Состояние банка
    if "состояние банка" in norm or (("банк" in norm) and ("мой" in norm or "мне" in norm) and not re.search(r"\d", norm)):
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
        if bank is None:
            return (
                "У тебя пока не задан банк.\n\n"
                "Можешь установить его командой вроде:\n"
                "`мой банк 100000`"
            )
        return f"Текущий банк: *{bank:,.0f}*".replace(",", " ")

    # 9) Установка банка
    if "банк" in norm:
        new_bank = _parse_bank_set(norm)
        if new_bank is not None:
            with db_session() as session:
                user = bets_db.set_user_bank(session, user_id, new_bank)
            return f"Банк установлен: *{user.bank:,.0f}*".replace(",", " ")

    # 10) Отчёт за неделю
    if "отчёт за неделю" in norm or "отчет за неделю" in norm:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        with db_session() as session:
            all_bets = bets_db.get_all_bets(session, user_id)
        last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
        return _format_week_report(last_week_bets)

    # 11) Разбор моих рынков
    if "разбор моих рынков" in norm:
        with db_session() as session:
            bets = bets_db.get_all_bets(session, user_id)

        if not bets:
            return (
                "У тебя пока нет сохранённых ставок, чтобы разобрать рынки.\n"
                "Начни фиксировать ставки — и я смогу показать, где ты зарабатываешь, а где сливаешь."
            )

        by_outcome: dict[str, float] = {}
        for b in bets:
            if not b.outcome:
                continue
            by_outcome.setdefault(b.outcome, 0.0)
            by_outcome[b.outcome] += float(b.profit or 0.0)

        if not by_outcome:
            return (
                "Ставки есть, но по ним пока мало структурированных данных.\n"
                "В следующих версиях будет полноценный разбор по рынкам и лигам."
            )

        lines = ["📉 *Разбор твоих рынков (MVP)*", ""]
        for outcome, pnl in sorted(by_outcome.items(), key=lambda x: -x[1]):
            lines.append(f"• {outcome}: *{pnl:+.0f}*")
        lines.append("")
        lines.append("_Это упрощённый разбор. В полной версии будет больше аналитики._")
        return "\n".join(lines)

    # 12) "ставка {id} выиграла/проиграла/возврат"
    m_res = re.match(r"ставка\s+(\d+)\s+(.+)", norm)
    if m_res:
        bet_id = int(m_res.group(1))
        result_text = m_res.group(2).strip()

        with db_session() as session:
            bet = bets_db.settle_bet(session, user_id, bet_id, result_text)

        if bet is None:
            return "Не удалось найти ставку или понять результат 😔"

        human = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}.get(bet.result or "", bet.result)
        pnl = bet.profit if bet.profit is not None else 0.0
        sign = "+" if pnl >= 0 else ""
        return f"Ставка #{bet.id} отмечена как *{human}*, PnL: *{sign}{pnl:.0f}*."

    # 13) Создание новой ставки (старый MVP формат)
    if norm.startswith("ставка"):
        body = text_raw.split("ставка", 1)[1]
        body = body.lstrip(" :")

        parts = [p.strip() for p in body.split(";") if p.strip()]
        event = None
        outcome = None
        stake = None
        odds = None

        if parts:
            first = parts[0].lower()
            if not any(key in first for key in ("исход", "сумма", "кэф", "коэф", "коэф.")):
                event = parts[0]

        for p in parts:
            pl = p.lower()
            if pl.startswith("исход"):
                outcome = p.split("=", 1)[-1].strip()
            elif pl.startswith("сумма") or pl.startswith("stake"):
                val = p.split("=", 1)[-1]
                val = re.sub(r"[^\d.,]", "", val).replace(",", ".")
                try:
                    stake = float(val)
                except ValueError:
                    pass
            elif pl.startswith("кэф") or pl.startswith("коэф") or pl.startswith("коэффициент"):
                val = p.split("=", 1)[-1]
                val = re.sub(r"[^\d.,]", "", val).replace(",", ".")
                try:
                    odds = float(val)
                except ValueError:
                    pass

        with db_session() as session:
            bet = bets_db.add_bet(
                session=session,
                user_id=user_id,
                raw_text=text_raw,
                stake=stake,
                odds=odds,
                event=event,
                outcome=outcome,
            )

        return (
            f"Ставка сохранена (id: {bet.id}).\n\n"
            "Когда узнаешь результат, нажми кнопку под ставкой или напиши:\n"
            f"`ставка {bet.id} выиграла` / `ставка {bet.id} проиграла` / `ставка {bet.id} возврат`."
        )

    # Help
    return (
        "Команды (MVP):\n\n"
        "• `матчи сегодня hockey|football|basketball|tennis|esports`\n"
        "• `матч <id>` → `рынок <id> <market>` → `аналитика <id> <market>`\n"
        "• `стратегия` — стратегия эксперта (по МСК)\n"
        "• `профиль`, `состояние банка`, `отчёт за неделю`, `разбор моих рынков`\n\n"
        "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
    )
