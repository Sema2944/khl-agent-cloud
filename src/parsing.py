# src/parsing.py
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlmodel import Session, SQLModel, Field, select

from .db import get_session
from . import bets_db
from .hockey_logic import khl_today_text_from_winline

logger = logging.getLogger(__name__)

# -----------------------------
# Экспертная стратегия (ENV fallback)
# -----------------------------
EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

# -----------------------------
# DB model: Expert Strategy (stored, updatable by admin)
# -----------------------------
class ExpertStrategy(SQLModel, table=True):
    __tablename__ = "expert_strategy"
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True)  # YYYY-MM-DD
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: int = Field(default=0, index=True)


# -----------------------------
# УТИЛИТА ДЛЯ SESSION (вне FastAPI)
# -----------------------------
@contextmanager
def db_session() -> Session:
    """
    Берём Session через общий dependency get_session() (yield-генератор).
    """
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
# ВСПОМОГАТЕЛЬНОЕ ФОРМАТИРОВАНИЕ
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
# Эксперт: хранение/чтение
# -----------------------------
def _get_strategy_from_db(session: Session, date_str: str) -> Optional[ExpertStrategy]:
    st = select(ExpertStrategy).where(ExpertStrategy.date == date_str).order_by(ExpertStrategy.updated_at.desc())
    return session.exec(st).first()

def _format_expert_strategy() -> str:
    """
    Показывает стратегию эксперта на сегодня:
    1) сначала пробуем из БД (если админ обновлял)
    2) потом ENV fallback
    """
    today = datetime.utcnow().date().isoformat()

    db_text = None
    db_date = today
    with db_session() as session:
        row = _get_strategy_from_db(session, today)
        if row and row.text:
            db_text = row.text
            db_date = row.date

    text = db_text or EXPERT_STRATEGY_TEXT
    date_label = db_date if db_text else (EXPERT_STRATEGY_DATE or today)

    if not text:
        return (
            "👤 *Стратегия эксперта на сегодня*\n"
            "_Пока не опубликована._\n\n"
            "Если ты админ — обнови командой:\n"
            "`админ стратегия: <текст>`"
        )

    lines = [
        "👤 *Стратегия эксперта на сегодня*",
        f"Дата: *{date_label}*",
        "",
        text,
        "",
        "_Дисклеймер: это аналитическая заметка, не призыв к ставке. Решение всегда на стороне пользователя._",
    ]
    return "\n".join(lines)


def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
    """
    Админ обновляет стратегию без ENV и без redeploy:
    - только ADMIN_TELEGRAM_ID
    - сохраняем в БД на сегодняшнюю дату
    """
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

    today = datetime.utcnow().date().isoformat()

    with db_session() as session:
        row = _get_strategy_from_db(session, today)
        if row is None:
            row = ExpertStrategy(
                date=today,
                text=new_text,
                updated_by=user_id,
                updated_at=datetime.utcnow(),
            )
            session.add(row)
        else:
            row.text = new_text
            row.updated_by = user_id
            row.updated_at = datetime.utcnow()
            session.add(row)
        session.commit()

    return True, (
        "✅ Стратегия обновлена и сохранена в БД.\n\n"
        "Теперь всем пользователям команда `стратегия` покажет обновлённый текст.\n"
        "ENV/Deploy не нужен."
    )


# -----------------------------
# Линия и нормализация (MVP демо)
# -----------------------------
def _normalize_demo_markets() -> list[dict]:
    """
    Заглушка нормализованных рынков (как в ТЗ).
    Позже сюда подставим адаптер OddsAPI/другого API.
    """
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

    # 1X2 / Moneyline
    ml = next((m for m in markets if m.get("type") == "moneyline"), None)
    if ml:
        lines.append("*1X2 / Moneyline*")
        lines.append(f"• Победа хозяев: *{ml['home']:.2f}*")
        if "draw" in ml:
            lines.append(f"• Ничья: *{ml['draw']:.2f}*")
        lines.append(f"• Победа гостей: *{ml['away']:.2f}*")
        lines.append("")

    # Total
    tot = next((m for m in markets if m.get("type") == "total"), None)
    if tot:
        lines.append("*Тотал*")
        lines.append(f"• Больше {tot['value']}: *{tot['over']:.2f}*")
        lines.append(f"• Меньше {tot['value']}: *{tot['under']:.2f}*")
        lines.append("")

    # Handicap
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
    """
    MVP-заглушка.
    prompt может быть: "<match_id>" или произвольный вопрос.
    """
    text = (prompt or "").strip()
    if not text:
        return "Напиши: `аналитика <id матча>` или `аналитика <вопрос>`"

    # Если похоже на id (без пробелов, коротко) — даём матчевую аналитику
    is_match_id = bool(re.fullmatch(r"[a-zA-Z0-9_\-:.]{6,80}", text))

    if is_match_id:
        markets = _normalize_demo_markets()
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
            "*Что можно проверить перед решением:*",
            "• состав/вратари, календарь, бэк-ту-бэк",
            "• стиль команд (темп, спецбригады), дисциплина",
            "• движение линии: резкие изменения кэфов перед матчем",
            "",
            "Если хочешь — напиши: `линия <id>` и я покажу рынки в структурированном виде.",
            "",
            "_Дисклеймер: аналитика не является призывом к ставке._",
        ]
        return "\n".join(lines)

    # Общий вопрос
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
        "Скоро подключим нормализацию рынков через OddsAPI/другой API и LLM-объяснения по конкретным матчам.",
        "",
        "_Дисклеймер: аналитика не является рекомендацией к ставке._",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА (MVP)
# ------------------------------------------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # 0) Админ: обновить стратегию
    if norm.startswith("админ"):
        ok, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # 0.1) Экспертная стратегия
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or norm.startswith("стратегия"):
        return _format_expert_strategy()

    # 0.2) Линия по матчу: "линия <id>"
    if norm.startswith("линия"):
        body = text_raw.split("линия", 1)[1].strip(" :\n\t")
        if not body:
            return "Напиши так: `линия <id>`"
        return _format_line(body)

    # 0.3) AI аналитика: "аналитика <id/вопрос>"
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

    # 8) Создание новой ставки (очень простой формат)
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
        "• `отчёт за неделю` — отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор рынков\n\n"
        "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
    )
    return help_text
