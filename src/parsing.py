# src/parsing.py
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from typing import Optional, List, Tuple

from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .db import get_session
from . import bets_db
from .hockey_logic import khl_today_text_from_winline
from .expert_db import ExpertStrategy  # <-- ВАЖНО: модель НЕ объявляем тут, чтобы не было дублей

logger = logging.getLogger(__name__)

# -----------------------------
# Настройки
# -----------------------------
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)
STRATEGY_TIMEZONE = (os.getenv("STRATEGY_TIMEZONE") or "Europe/Moscow").strip()


# -----------------------------
# Session helper (вне FastAPI)
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
# Time helpers
# -----------------------------
def _today_local_iso() -> str:
    """
    Сегодняшняя дата в заданном часовом поясе (по умолчанию МСК).
    """
    try:
        tz = ZoneInfo(STRATEGY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(tz).date().isoformat()


def _parse_date_token(s: str) -> Optional[str]:
    """
    Принимает YYYY-MM-DD, возвращает строку YYYY-MM-DD или None.
    """
    s = (s or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        # валидируем как дату
        date.fromisoformat(s)
        return s
    except Exception:
        return None


# -----------------------------
# UI helpers
# -----------------------------
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
# Экспертная стратегия: DB
# -----------------------------
def _get_strategy_by_date(session: Session, date_iso: str) -> Optional[ExpertStrategy]:
    st = (
        select(ExpertStrategy)
        .where(ExpertStrategy.date == date.fromisoformat(date_iso))
        .order_by(ExpertStrategy.updated_at.desc())
    )
    return session.exec(st).first()


def _get_latest_strategy(session: Session) -> Optional[ExpertStrategy]:
    st = (
        select(ExpertStrategy)
        .order_by(ExpertStrategy.date.desc(), ExpertStrategy.updated_at.desc())
    )
    return session.exec(st).first()


def _format_strategy_row(row: ExpertStrategy, title: str = "👤 *Стратегия эксперта*") -> str:
    date_label = row.date.isoformat()
    lines = [
        title,
        f"Дата: *{date_label}*",
        "",
        row.text.strip(),
        "",
        "_Дисклеймер: это аналитическая заметка, не призыв к ставке. Решение всегда на стороне пользователя._",
    ]
    return "\n".join(lines)


def _format_expert_strategy(requested_date: Optional[str] = None, want_latest: bool = False) -> str:
    """
    Команды:
    - "стратегия" -> стратегия на сегодня (по TZ)
      если нет -> покажет последнюю (с пометкой)
    - "стратегия последняя" -> последняя
    - "стратегия 2025-12-24" -> конкретная дата
    """
    today_iso = _today_local_iso()

    with db_session() as session:
        if want_latest:
            latest = _get_latest_strategy(session)
            if not latest:
                return (
                    "👤 *Стратегия эксперта*\n"
                    "_Пока не опубликована._"
                )
            return _format_strategy_row(latest, title="👤 *Стратегия эксперта (последняя)*")

        if requested_date:
            row = _get_strategy_by_date(session, requested_date)
            if not row:
                return (
                    "👤 *Стратегия эксперта*\n"
                    f"На дату *{requested_date}* стратегии нет."
                )
            return _format_strategy_row(row, title="👤 *Стратегия эксперта*")

        # default: today
        row_today = _get_strategy_by_date(session, today_iso)
        if row_today:
            return _format_strategy_row(row_today, title="👤 *Стратегия эксперта на сегодня*")

        # fallback: latest
        latest = _get_latest_strategy(session)
        if latest:
            return _format_strategy_row(
                latest,
                title="👤 *Стратегия эксперта* (сегодня ещё не обновлена — показываю последнюю)"
            )

    return (
        "👤 *Стратегия эксперта на сегодня*\n"
        "_Пока не опубликована._\n\n"
        "Админ может обновить командой:\n"
        "`админ стратегия: <текст>`"
    )


def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
    """
    Админ обновляет стратегию на "сегодня" (по TZ).
    """
    if ADMIN_TELEGRAM_ID <= 0:
        return False, "ADMIN_TELEGRAM_ID не задан в окружении backend."

    if user_id != ADMIN_TELEGRAM_ID:
        return False, "Доступ запрещён."

    m = re.match(r"админ\s+стратегия\s*:\s*(.+)$", raw_text.strip(), re.IGNORECASE | re.DOTALL)
    if not m:
        return False, "Неверный формат. Пример: `админ стратегия: текст...`"

    new_text = m.group(1).strip()
    if not new_text:
        return False, "Пустой текст стратегии."

    today_iso = _today_local_iso()
    today_dt = date.fromisoformat(today_iso)

    with db_session() as session:
        row = _get_strategy_by_date(session, today_iso)
        now = datetime.utcnow()

        if row is None:
            row = ExpertStrategy(
                date=today_dt,
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

    return True, (
        "✅ Стратегия обновлена и сохранена в БД.\n\n"
        "Команда `стратегия` покажет её всем пользователям.\n"
        f"(TZ: {STRATEGY_TIMEZONE}, дата: {today_iso})"
    )


# -----------------------------
# Линия/нормализация (MVP демо)
# -----------------------------
def _normalize_demo_markets() -> list[dict]:
    return [
        {"type": "moneyline", "home": 1.85, "draw": 3.90, "away": 2.10},
        {"type": "total", "value": 5.5, "over": 1.87, "under": 1.95},
        {"type": "handicap", "team": "home", "value": -1.5, "odds": 2.35},
    ]


def _format_line(match_id: str) -> str:
    markets = _normalize_demo_markets()
    lines: list[str] = []
    lines.append("📈 *Линия (MVP / демо)*")
    lines.append(f"Матч id: `{match_id}`")
    lines.append("")

    ml = next((m for m in markets if m.get("type") == "moneyline"), None)
    if ml:
        lines.append("*1X2 / Moneyline*")
        lines.append(f"• Победа хозяев: *{ml['home']:.2f}*")
        if "draw" in ml:
            lines.append(f"• Ничья: *{ml['draw']:.2f}*")
        lines.append(f"• Победа гостей: *{ml['away']:.2f}*")
        lines.append("")

    tot = next((m for m in markets if m.get("type") == "total"), None)
    if tot:
        lines.append("*Тотал*")
        lines.append(f"• Больше {tot['value']}: *{tot['over']:.2f}*")
        lines.append(f"• Меньше {tot['value']}: *{tot['under']:.2f}*")
        lines.append("")

    hc = next((m for m in markets if m.get("type") == "handicap"), None)
    if hc:
        team = "Хозяева" if hc.get("team") == "home" else "Гости"
        val = hc.get("value")
        odds = hc.get("odds")
        lines.append("*Фора*")
        lines.append(f"• {team} {val:+.1f}: *{odds:.2f}*")
        lines.append("")

    lines.append("_Дисклеймер: линия показана для объяснения рынков. Не является рекомендацией._")
    return "\n".join(lines)


# -----------------------------
# AI аналитика (MVP)
# -----------------------------
async def ai_analyze(user_id: int, prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return "Напиши: `аналитика <id матча>` или `аналитика <вопрос>`"

    is_match_id = bool(re.fullmatch(r"[a-zA-Z0-9_\-:.]{6,80}", text))

    if is_match_id:
        lines = [
            "🧠 *AI аналитика (MVP)*",
            f"Матч id: `{text}`",
            "",
            "Цель: объяснить рынки и логику коэффициентов — без прямых рекомендаций.",
            "",
            "*Как читать рынки:*",
            "• 1X2/Moneyline: кэф ниже → событие считается более вероятным.",
            "• Тотал: ключевой порог (например 5.5) — ожидание результативности.",
            "• Фора: виртуальное преимущество/отставание меняет условия прохода.",
            "",
            "Напиши: `линия <id>` — покажу рынки структурировано.",
            "",
            "_Дисклеймер: аналитика не является призывом к ставке._",
        ]
        return "\n".join(lines)

    lines = [
        "🧠 *AI аналитика (MVP)*",
        "",
        f"Запрос: _{text}_",
        "",
        "Пока LLM не подключён. Но я могу объяснять базовые вещи:",
        "• как читать коэффициенты и имплицитные вероятности",
        "• чем отличаются тоталы, форы, moneyline",
        "• как мыслить сценариями матча (темп, дисциплина, спецбригады)",
        "",
        "_Дисклеймер: аналитика не является рекомендацией к ставке._",
    ]
    return "\n".join(lines)


# -----------------------------
# Основной агент
# -----------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # 0) Админ: обновить стратегию
    if norm.startswith("админ"):
        ok, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # 0.1) Стратегия
    if norm.startswith("стратег"):
        # варианты:
        # "стратегия"
        # "стратегия последняя"
        # "стратегия 2025-12-24"
        tail = norm.split(maxsplit=1)[1].strip() if len(norm.split()) > 1 else ""
        if tail in {"последняя", "последняя."}:
            return _format_expert_strategy(want_latest=True)

        date_tok = _parse_date_token(tail) if tail else None
        return _format_expert_strategy(requested_date=date_tok, want_latest=False)

    # 0.2) Линия
    if norm.startswith("линия"):
        body = text_raw.split("линия", 1)[1].strip(" :\n\t")
        if not body:
            return "Напиши так: `линия <id>`"
        return _format_line(body)

    # 0.3) Аналитика
    if norm.startswith("аналитика"):
        body = text_raw.split("аналитика", 1)[1].strip(" :\n\t")
        return await ai_analyze(user_id=user_id, prompt=body)

    # 1) Профиль
    if "профиль" in norm:
        with db_session() as session:
            bank = bets_db.get_user_bank(session, user_id)
            stats = bets_db.get_user_stats(session, user_id)
        return _format_profile_text(bank, stats)

    # 2) Состояние банка
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

    # 3) Установка банка
    if "банк" in norm:
        new_bank = _parse_bank_set(norm)
        if new_bank is not None:
            with db_session() as session:
                user = bets_db.set_user_bank(session, user_id, new_bank)
            return f"Банк установлен: *{user.bank:,.0f}*".replace(",", " ")

    # 4) Отчёт за неделю
    if "отчёт за неделю" in norm or "отчет за неделю" in norm:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        with db_session() as session:
            all_bets = bets_db.get_all_bets(session, user_id)
        last_week_bets = [b for b in all_bets if b.created_at >= week_ago]
        return _format_week_report(last_week_bets)

    # 5) КХЛ сегодня
    if "кхл сегодня" in norm or "кхл на сегодня" in norm:
        try:
            text = await khl_today_text_from_winline()
            if not text:
                return "Пока не вижу линию КХЛ на сегодня. Попробуй чуть позже."
            return text
        except Exception as e:
            logger.exception("khl_today_text_from_winline failed: %s", e)
            return (
                "🏒 Матчи КХЛ на сегодня:\n\n"
                "1) СКА — ЦСКА (id: demo_khl_123456)\n\n"
                "Чтобы получить линию, напиши: `линия <id>`"
            )

    # 6) Разбор моих рынков
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

        lines = ["📊 *Разбор твоих рынков (MVP)*", ""]
        for outcome, pnl in sorted(by_outcome.items(), key=lambda x: -x[1]):
            lines.append(f"• {outcome}: *{pnl:+.0f}*")

        lines.append("")
        lines.append("_Это упрощённый разбор. В полной версии будет больше аналитики._")
        return "\n".join(lines)

    # 7) "ставка {id} выиграла/проиграла/возврат"
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

    # 8) Создание новой ставки
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

    # 9) Help
    help_text = (
        "Я понимаю команды:\n\n"
        "• `профиль` — статистика и банк\n"
        "• `состояние банка` — текущий банк\n"
        "• `мой банк 100000` — установить банк\n"
        "• `КХЛ сегодня` — матчи на сегодня\n"
        "• `линия <id>` — рынки/коэффициенты (MVP)\n"
        "• `аналитика <id/вопрос>` — AI аналитика (MVP)\n"
        "• `стратегия` — стратегия эксперта на сегодня\n"
        "• `стратегия последняя` — последняя стратегия\n"
        "• `стратегия YYYY-MM-DD` — стратегия на дату\n"
        "• `отчёт за неделю` — отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор рынков\n\n"
        "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
    )
    return help_text
