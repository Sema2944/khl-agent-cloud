# src/parsing.py
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlmodel import Session

from .db import get_session
from . import bets_db
from .hockey_logic import khl_today_text_from_winline

logger = logging.getLogger(__name__)

# -----------------------------
# Экспертная стратегия (MVP)
# -----------------------------
EXPERT_STRATEGY_TEXT = (os.getenv("EXPERT_STRATEGY_TEXT") or "").strip()
EXPERT_STRATEGY_DATE = (os.getenv("EXPERT_STRATEGY_DATE") or "").strip()  # YYYY-MM-DD
ADMIN_TELEGRAM_ID = int((os.getenv("ADMIN_TELEGRAM_ID") or "0").strip() or 0)

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
    """
    Поймать команды вроде:
    - "мой банк 100000"
    - "банк 200 000"
    """
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
    lines.append("_Это базовый отчёт MVP. Позже я буду давать разбор по лигам и рынкам._")

    return "\n".join(lines)


# -----------------------------
# Эксперт: формат вывода
# -----------------------------
def _format_expert_strategy() -> str:
    """
    Показывает стратегию эксперта на сегодня из ENV.
    """
    today = datetime.utcnow().date().isoformat()

    if not EXPERT_STRATEGY_TEXT:
        return (
            "👤 *Стратегия эксперта на сегодня*\n"
            "_Пока не опубликована._\n\n"
            "Админ может обновить стратегию командой:\n"
            "`админ стратегия: <текст>`"
        )

    # Если задана дата — показываем явно, чтобы не путать "вчерашнее"
    if EXPERT_STRATEGY_DATE:
        date_label = EXPERT_STRATEGY_DATE
    else:
        date_label = today

    lines = [
        "👤 *Стратегия эксперта на сегодня*",
        f"Дата: *{date_label}*",
        "",
        EXPERT_STRATEGY_TEXT,
        "",
        "_Дисклеймер: это аналитическая заметка, не призыв к ставке. Решение всегда на стороне пользователя._",
    ]
    return "\n".join(lines)


def _try_admin_update_strategy(user_id: int, raw_text: str) -> Tuple[bool, str]:
    """
    MVP: обновление стратегии командой админа.
    Технически ENV на Render мы не можем менять из кода.
    Поэтому делаем "мягкий" режим:
    - разрешаем только админскому ID
    - сохраняем стратегию как ставку-заметку в БД? (не надо)
    - проще: возвращаем понятный текст, что нужно обновить ENV и redeploy.
    """
    if ADMIN_TELEGRAM_ID <= 0:
        return False, "ADMIN_TELEGRAM_ID не задан в окружении."

    if user_id != ADMIN_TELEGRAM_ID:
        return False, "Доступ запрещён."

    # извлекаем текст после "админ стратегия:"
    m = re.match(r"админ\s+стратегия\s*:\s*(.+)$", raw_text.strip(), re.IGNORECASE | re.DOTALL)
    if not m:
        return False, "Неверный формат. Пример: `админ стратегия: текст...`"

    new_text = m.group(1).strip()
    if not new_text:
        return False, "Пустой текст стратегии."

    # Пояснение: ENV меняется вручную на Render
    return True, (
        "✅ Команда принята.\n\n"
        "⚠️ Важно: бот не может сам изменить переменные окружения на Render.\n"
        "Обнови в Render → Environment:\n"
        f"• EXPERT_STRATEGY_TEXT = (твой текст)\n"
        f"• EXPERT_STRATEGY_DATE = {datetime.utcnow().date().isoformat()}\n"
        "и сделай Redeploy.\n\n"
        "После redeploy команда `стратегия` покажет обновлённый текст."
    )


# -----------------------------
# AI аналитика (пока заглушка, архитектура готова)
# -----------------------------
async def ai_analyze(user_id: int, prompt: str) -> str:
    """
    MVP-заглушка для AI-аналитики.
    Позже заменим на реальный LLM (OpenAI/другой) + нормализатор рынков.
    """
    # Здесь мы уже можем сделать полезный ответ без LLM:
    # - объяснение линии/кэфов
    # - напоминание про риск
    text = prompt.strip()
    if not text:
        return "Напиши: `аналитика <матч/рынок/вопрос>`"

    lines = [
        "🧠 *AI аналитика (MVP)*",
        "",
        f"Запрос: _{text}_",
        "",
        "Пока LLM не подключён. Но я уже могу объяснять линию и структуру рынков:",
        "• 1X2 / Moneyline — победа/ничья/победа (или победа хозяев/гостей)",
        "• Тотал — больше/меньше указанного значения",
        "• Фора — виртуальное преимущество/отставание, меняет условия выигрыша",
        "",
        "Когда подключим OddsAPI и LLM, я буду давать разбор факторов и сценариев именно под твой запрос.",
        "",
        "_Дисклеймер: аналитика не является рекомендацией к ставке._",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АГЕНТА (MVP)
# ------------------------------------------------------------
async def run_dialog_agent(user_id: int, message: str) -> str:
    """
    Главная функция «мозга» бота (MVP).
    """
    text_raw = message or ""
    norm = text_raw.lower().strip()

    logger.info("run_dialog_agent: user_id=%s, norm=%r", user_id, norm)

    # 0) Админ: обновить стратегию
    if norm.startswith("админ"):
        ok, msg = _try_admin_update_strategy(user_id, text_raw)
        return msg

    # 0.1) Экспертная стратегия
    if norm in {"стратегия", "эксперт", "эксперт сегодня", "стратегия сегодня"} or "стратегия" in norm:
        return _format_expert_strategy()

    # 0.2) AI аналитика
    # Команда: "аналитика ...."
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
            # fallback (демо)
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
                "В следующих версиях я буду делать полноценный разбор по рынкам, лигам и типам ставок."
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

        human = {"win": "выигрыш", "lose": "проигрыш", "push": "возврат"}.get(
            bet.result or "", bet.result
        )
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

    # 9) Дефолтный help
    help_text = (
        "Я понимаю команды:\n\n"
        "• `профиль` — статистика и банк\n"
        "• `состояние банка` — текущий банк\n"
        "• `мой банк 100000` — установить банк\n"
        "• `КХЛ сегодня` — матчи на сегодня\n"
        "• `отчёт за неделю` — отчёт по ставкам\n"
        "• `разбор моих рынков` — базовый разбор рынков\n"
        "• `ставка: <событие>; исход=...; сумма=...; кэф=...` — сохранить ставку\n"
        "• `стратегия` — стратегия эксперта на сегодня\n"
        "• `аналитика <вопрос>` — AI аналитика (пока MVP)\n\n"
        "_Дисклеймер: сервис даёт аналитику, а не рекомендации к ставкам._"
    )
    return help_text
