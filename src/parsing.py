from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

from sqlmodel import Session, select

from .db import get_session
from . import bets_db
from .expert_db import ExpertStrategy  # <- ВАЖНО: модель только здесь, НЕ объявляем в parsing.py

logger = logging.getLogger(__name__)

# =============================
# TIMEZONE: стратегия "на сегодня" по МСК
# =============================
MSK = timezone(timedelta(hours=3))


def today_msk_date():
    return datetime.now(MSK).date()


def today_msk_str() -> str:
    return today_msk_date().isoformat()


# =============================
# ADMIN / ENV fallback
# =============================
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

# ENV fallback (если БД пустая)
EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD


# =============================
# DB Session helper
# =============================
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


# =============================
# Helpers
# =============================
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


# =============================
# Expert strategy: DB get/set
# =============================
def _get_strategy_from_db(session: Session, date_obj) -> Optional[ExpertStrategy]:
    q = (
        select(ExpertStrategy)
        .where(ExpertStrategy.date == date_obj)
        .order_by(ExpertStrategy.updated_at.desc())
    )
    return session.exec(q).first()


def _format_expert_strategy() -> str:
    today = today_msk_date()

    db_text: Optional[str] = None
    with db_session() as session:
        row = _get_strategy_from_db(session, today)
        if row and row.text:
            db_text = row.text

    # fallback на ENV
    text = db_text or EXPERT_STRATEGY_TEXT
    date_label = today.isoformat() if db_text else (EXPERT_STRATEGY_DATE or today.isoformat())

    if not text:
        return (
            "👤 *Стратегия эксперта на сегодня*\n"
            "_Пока не опубликована._\n\n"
            "Если ты админ — обнови командой:\n"
            "`админ стратегия: <текст>`"
        )

    return "\n".join(
        [
            "👤 *Стратегия эксперта на сегодня*",
            f"Дата (МСК): *{date_label}*",
            "",
            text,
            "",
            "_Дисклеймер: это аналитическая заметка, не призыв к ставке._",
        ]
    )


def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
    if ADMIN_TELEGRAM_ID <= 0:
        return False, "ADMIN_TELEGRAM_ID не задан в окружении."

    if user_id != ADMIN_TELEGRAM_ID:
        return False, "Доступ запрещён."

    m = re.match(r"админ\s+стратегия\s*:\s*(.+)$", raw_text.strip(), re.IGNORECASE | re.DOTALL)
    if not m:
        return False, "Неверный формат. Пример: `админ стратегия: текст...`"

    new_text = m.group(1).strip()
    if not new_text:
        return False, "Пустой текст стратегии."

    today = today_msk_date()

    with db_session() as session:
        row = _get_strategy_from_db(session, today)
        if row is None:
            row = ExpertStrategy(date=today, text=new_text, updated_by=user_id, updated_at=datetime.utcnow())
            session.add(row)
        else:
            row.text = new_text
            row.updated_by = user_id
            row.updated_at = datetime.utcnow()
            session.add(row)

        session.commit()

    return True, "✅ Стратегия обновлена и сохранена в БД (дата по МСК)."


# =============================
# Matches today (DEMO)
# =============================
def _matches_today_demo_text() -> str:
    matches = [
        {"id": "demo_hockey_001", "sport": "Хоккей", "league": "КХЛ", "title": "СКА — ЦСКА"},
        {"id": "demo_football_001", "sport": "Футбол", "league": "РПЛ", "title": "Зенит — Спартак"},
        {"id": "demo_basket_001", "sport": "Баскетбол", "league": "Евролига", "title": "ЦСКА — Реал"},
    ]
    lines = ["🏟 *Матчи сегодня (MVP / демо)*", ""]
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}) {m['sport']} / {m['league']}: *{m['title']}* (id: `{m['id']}`)")
    lines.append("")
    lines.append("Команды: `линия <id>` / `аналитика <id>` / `стратегия`")
    return "\n".join(lines)


# =============================
# Markets normalization (DEMO)
# =============================
def _normalize_demo_markets() -> list[dict]:
    return [
        {"type": "moneyline", "home": 1.85, "draw": 3.90, "away": 2.10},
        {"type": "total", "value": 5.5, "over": 1.87, "under": 1.95},
        {"type": "handicap", "team": "home", "value": -1.5, "odds": 2.35},
    ]


def _format_line(match_id: str) -> str:
    markets = _normalize_demo_markets()
    out: list[str] = ["📈 *Линия (MVP / демо)*", f"Матч id: `{match_id}`", ""]

    ml = next((m for m in markets if m.get("type") == "moneyline"), None)
    if ml:
        out += [
            "*1X2 / Moneyline*",
            f"• Победа хозяев: *{ml['home']:.2f}*",
            f"• Ничья: *{ml['draw']:.2f}*",
            f"• Победа гостей: *{ml['away']:.2f}*",
            "",
        ]

    tot = next((m for m in markets if m.get("type") == "total"), None)
    if tot:
        out += [
            "*Тотал*",
            f"• Больше {tot['value']}: *{tot['over']:.2f}*",
            f"• Меньше {tot['value']}: *{tot['under']:.2f}*",
            "",
        ]

    hc = next((m for m in markets if m.get("type") == "handicap"), None)
    if hc:
        team = "Хозяева" if hc.get("team") == "home" else "Гости"
        out += [
            "*Фора*",
            f"• {team} {hc['value']:+.1f}: *{hc['odds']:.2f}*",
            "",
        ]

    out.append("_Дисклеймер: линия для объяснения рынков. Не рекомендация._")
    return "\n".join(out)


# =============================
# AI analysis (MVP placeholder)
# =============================
async def ai_analyze(user_id: int, prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return "Напиши: `аналитика <id матча>` или `аналитика <вопрос>`"

    is_match_id = bool(re.fullmatch(r"[a-zA-Z0-9_\-:.]{6,80}", text))
    if is_match_id:
        return "\n".join(
            [
                "🧠 *AI аналитика (MVP)*",
                f"Матч id: `{text}`",
                "",
                "Цель: объяснение рынков и логики коэффициентов — без прямых рекомендаций.",
                "",
                "*Что можно проверить:*",
                "• состав/травмы/вратари",
                "• календарь и усталость",
                "• стиль команд, дисциплина",
                "• движение линии (сдвиги кэфов)",
                "",
                "Хочешь рынки: `линия <id>`",
                "",
                "_Дисклеймер: аналитика не является рекомендацией._",
            ]
        )

    return "\n".join(
        [
            "🧠 *AI аналитика (MVP)*",
            "",
            f"Запрос: _{text}_",
            "",
            "Пока LLM не подключён. Я могу объяснять:",
            "• как читать коэффициенты",
            "• тоталы/форы/moneyline",
            "• как мыслить сценариями матча",
            "",
            "_Дисклеймер: аналитика не является рекомендацией._",
        ]
    )


# =============================
# MAIN agent
# =============================
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s norm=%r", user_id, norm)

    # admin strategy
    if norm.startswith("админ"):
        _, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # expert strategy
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _format_expert_strategy()

    # matches today
    if norm in {"матчи сегодня", "матчи", "сегодня матчи"}:
        return _matches_today_demo_text()

    # line
    if norm.startswith("линия"):
        body = text_raw.split("линия", 1)[1].strip(" :\n\t")
        if not body:
            return "Напиши так: `линия <id>`"
        return _format_line(body)

    # ai
    if norm.startswith("аналитика"):
        body = text_raw.split("аналитика", 1)[1].strip(" :\n\t")
        return await ai_analyze(user_id=user_id, prompt=body)

    # profile
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _format_profile_text(bank, stats)

    # bank status
    if "состояние банка" in norm or (("банк" in norm) and ("мой" in norm or "мне" in norm) and not re.search(r"\d", norm)):
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
        if bank is None:
            return "У тебя пока не задан банк.\n\nМожешь установить командой: `мой банк 100000`"
        return f"Текущий банк: *{bank:,.0f}*".replace(",", " ")

    # set bank
    if "банк" in norm:
        new_bank = _parse_bank_set(norm)
        if new_bank is not None:
            with db_session() as session:
                user = bets_db.set_user_bank(session, user_id, new_bank)
            return f"Банк установлен: *{user.bank:,.0f}*".replace(",", " ")

    # week report
    if "отчёт за неделю" in norm or "отчет за неделю" in norm:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        with db_session() as session:
            all_bets = bets_db.get_all_bets(session, user_id)
        last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
        return _format_week_report(last_week_bets)

    # markets breakdown
    if "разбор моих рынков" in norm:
        with db_session() as session:
            bets = bets_db.get_all_bets(session, user_id)

        if not bets:
            return (
                "У тебя пока нет сохранённых ставок, чтобы разобрать рынки.\n"
                "Начни фиксировать ставки — и я покажу, где ты зарабатываешь, а где сливаешь."
            )

        by_outcome: dict[str, float] = {}
        for b in bets:
            if not b.outcome:
                continue
            by_outcome.setdefault(b.outcome, 0.0)
            by_outcome[b.outcome] += float(b.profit or 0.0)

        if not by_outcome:
            return "Ставки есть, но по ним мало структурированных данных."

        lines = ["📊 *Разбор твоих рынков (MVP)*", ""]
        for outcome, pnl in sorted(by_outcome.items(), key=lambda x: -x[1]):
            lines.append(f"• {outcome}: *{pnl:+.0f}*")
        lines.append("")
        lines.append("_Это упрощённый разбор. В полной версии будет больше аналитики._")
        return "\n".join(lines)

    # settle bet
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

    # add bet
    if norm.startswith("ставка"):
        body = text_raw.split("ставка", 1)[1].lstrip(" :")
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
                val = re.sub(r"[^\d.,]", "", p.split("=", 1)[-1]).replace(",", ".")
                try:
                    stake = float(val)
                except ValueError:
                    pass
            elif pl.startswith("кэф") or pl.startswith("коэф") or pl.startswith("коэффициент"):
                val = re.sub(r"[^\d.,]", "", p.split("=", 1)[-1]).replace(",", ".")
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
            "Когда узнаешь результат, напиши:\n"
            f"`ставка {bet.id} выиграла` / `ставка {bet.id} проиграла` / `ставка {bet.id} возврат`."
        )

    # help
    return (
        "Команды:\n\n"
        "• `матчи сегодня` — список матчей (MVP)\n"
        "• `линия <id>` — рынки/коэффициенты (MVP)\n"
        "• `аналитика <id/вопрос>` — AI аналитика (MVP)\n"
        "• `стратегия` — стратегия эксперта на сегодня (по МСК)\n"
        "• `профиль` — статистика и банк\n"
        "• `состояние банка` — текущий банк\n"
        "• `мой банк 100000` — установить банк\n"
        "• `отчёт за неделю` — отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор\n\n"
        "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
    )
