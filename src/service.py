# src/service.py

import logging
import threading
import os
import re

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session
from .bets_db import (
    get_user_stats,
    add_bet,
    get_last_bets,
    settle_bet,
)
from .khl_client import get_today_khl_events

logger = logging.getLogger(__name__)

app = FastAPI(title="KHL AI Betting Agent")


class AgentQuery(BaseModel):
    user_id: int
    message: str


class AgentResponse(BaseModel):
    reply: str


@app.on_event("startup")
def on_startup() -> None:
    """
    Хук старта FastAPI:
    - инициализируем БД
    - настраиваем базовые логи
    """
    logging.basicConfig(level=logging.INFO)
    init_db()
    logger.info("FastAPI сервис запущен")


@app.get("/")
def root():
    return {"status": "ok", "service": "khl-agent"}


@app.post("/agent/query", response_model=AgentResponse)
async def agent_query(
    payload: AgentQuery,
    session: Session = Depends(get_session),
) -> AgentResponse:
    """
    Главная точка входа для AI-агента.
    Telegram-бот (и любые клиенты) шлют сюда user_id + текст.
    """
    reply_text = await run_agent(
        user_id=payload.user_id,
        message=payload.message,
        session=session,
    )
    return AgentResponse(reply=reply_text)


# ------------------ ЛОГИКА АГЕНТА ------------------


async def run_agent(user_id: int, message: str, session: Session) -> str:
    """
    Простейший if/else-агент.
    Дальше сюда можно будет наворачивать более умную логику и LLM.
    """
    original_text = message or ""
    text = original_text.lower().strip()

    # 0) Отметить результат ставки: "ставка 1 выиграла" / "ставка 2 проиграла"
    m_res = re.search(
        r"ставка\s+(\d+)\s+(выиграл[аи]?|проиграл[аи]?|выигрыш|проигрыш|win|lose|loss)",
        text,
    )
    if m_res:
        bet_id = int(m_res.group(1))
        res_word = m_res.group(2)

        result = res_word  # settle_bet сам нормализует
        bet = settle_bet(session, user_id, bet_id, result)
        if bet is None:
            return f"Не нашёл ставку с id {bet_id} или не понял результат."

        human_res = "выигрыш" if bet.result == "win" else "проигрыш"
        msg = f"Отметил ставку {bet.id} как {human_res}."
        if bet.profit is not None:
            sign = "+" if bet.profit >= 0 else ""
            msg += f"\nРезультат по сумме: {sign}{bet.profit:.0f}."
        msg += "\n\nПосмотреть обновлённую статистику: 'Покажи мою статистику'."
        return msg

    # 1) Показать статистику по ставкам
    if "статист" in text or "статку" in text or "stats" in text:
        stats = get_user_stats(session, user_id)
        if stats.total_bets == 0:
            return "Пока нет ни одной сохранённой ставки. Начнём с первой 😉"

        if stats.settled_bets == 0:
            return (
                f"Всего ставок: {stats.total_bets}\n"
                "Пока ни одна ставка не рассчитана.\n"
                "Когда отметишь результаты (например: 'ставка 1 выиграла'), "
                "я посчитаю winrate и ROI."
            )

        return (
            "Твоя статистика:\n"
            f"Всего ставок: {stats.total_bets}\n"
            f"Рассчитано: {stats.settled_bets}\n"
            f"Винрейт: {stats.winrate:.1f}%\n"
            f"ROI: {stats.roi:.2f}%\n"
            f"Плюс/минус: {stats.pnl:.0f}\n"
            f"Общий объём ставок: {stats.total_stake:.0f}"
        )

    # 2) Матчи КХЛ на сегодня (через парсер/Winline или заглушку)
    if "кхл" in text and ("сегодня" in text or "на сегодня" in text):
        try:
            events = await get_today_khl_events()
        except Exception:
            # Логируем стек ошибки, но пользователю отдаём аккуратный текст
            logger.exception("Ошибка при получении матчей КХЛ")
            return (
                "Не смог получить матчи КХЛ из источника "
                "(ошибка парсера или API бука).\n"
                "Попробуй ещё раз чуть позже или сформулируй другой запрос."
            )

        if not events:
            return "На сегодня я не нашёл матчей КХЛ."

        lines = []
        for e in events[:5]:  # ограничимся первыми 5 матчами
            line = f"{e.team1} — {e.team2} (id: {e.id})"

            # Пытаемся найти рынок 1X2 и показать коэффициенты
            market_1x2 = next((m for m in e.markets if m.name == "1X2"), None)
            if market_1x2:
                odds_part = ", ".join(
                    f"{o.name}: {o.price}" for o in market_1x2.outcomes
                )
                line += f" | 1X2: {odds_part}"

            lines.append(line)

        return "Матчи КХЛ на сегодня:\n" + "\n".join(lines)

    # 3) Показать последние ставки: "мои ставки"
    if "мои ставки" in text or ("ставки" in text and "мои" in text):
        from .bets_db import Bet  # чтобы взять result/profit

        bets = get_last_bets(session, user_id, limit=5)
        if not bets:
            return "У тебя пока нет сохранённых ставок."

        lines = []
        for b in bets:
            line = f"{b.created_at:%d.%m %H:%M} — {b.raw_text}"
            if b.stake:
                line += f" | сумма: {b.stake:g}"
            if b.odds:
                line += f" | кэф: {b.odds:.2f}"
            if b.result:
                human_res = "выигрыш" if b.result == "win" else "проигрыш"
                line += f" | результат: {human_res}"
            if b.profit is not None:
                sign = "+" if b.profit >= 0 else ""
                line += f" | PnL: {sign}{b.profit:.0f}"
            lines.append(line)

        return "Твои последние ставки:\n" + "\n".join(lines)

    # 4) Добавление ставки: сообщения, начинающиеся со слова "ставка"
    if text.startswith("ставка"):
        # Оригинальный текст лучше взять без .lower(), чтобы не терять регистр:
        raw_text = original_text.strip()

        # Ищем все числа: первое — считаем суммой, второе (если есть) — кэф
        # Пример: "ставка 1000 на СКА победа за 1.85"
        num_matches = re.findall(r"(\d+([\.,]\d+)?)", raw_text)
        numbers = [m[0] for m in num_matches]

        stake = None
        odds = None

        if numbers:
            # первое число — сумма ставки
            try:
                stake = float(numbers[0].replace(",", "."))
            except ValueError:
                stake = None

        if len(numbers) >= 2:
            # второе число — коэффициент
            try:
                candidate_odds = float(numbers[1].replace(",", "."))
                # небольшой фильтр: кэф обычно >= 1.01
                if candidate_odds >= 1.01:
                    odds = candidate_odds
            except ValueError:
                odds = None

        bet = add_bet(
            session=session,
            user_id=user_id,
            raw_text=raw_text,
            stake=stake,
            odds=odds,
        )

        resp = f"Ставка сохранена (id: {bet.id}).\n\nТекст: {bet.raw_text}"
        if stake is not None:
            resp += f"\nСумма: {stake:g}"
        if odds is not None:
            resp += f"\nКоэффициент: {odds:.2f}"
        resp += (
            "\n\nКогда узнаешь результат, напиши, например:\n"
            f"'ставка {bet.id} выиграла' или 'ставка {bet.id} проиграла'.\n"
            "Посмотреть: 'мои ставки' или 'Покажи мою статистику'."
        )
        return resp

    # 5) Ответ по умолчанию
    return (
        "Я AI-агент для ставок.\n"
        "Сейчас умею:\n"
        "• По словам 'статистика / статку' показывать твою статистику\n"
        "• По запросу 'КХЛ сегодня' показывать матчи КХЛ\n"
        "• По сообщению вида 'ставка ...' сохранять ставку в базу\n"
        "• По запросу 'мои ставки' показывать последние сохранённые\n"
        "• По фразе 'ставка N выиграла/проиграла' отмечать результат и считать winrate/ROI\n\n"
        "Попробуй, например:\n"
        "• 'ставка 1000 на СКА победа за 1.85'\n"
        "• 'мои ставки'\n"
        "• 'ставка 1 выиграла'\n"
        "• 'Покажи мою статистику'\n"
        "• 'Какие матчи КХЛ сегодня?'\n"
    )


# ------------------ ЗАПУСК TELEGRAM-БОТА В ФОНЕ ------------------


def _start_bot_background() -> None:
    """
    Стартуем Telegram-бота в отдельном потоке.
    Если TELEGRAM_BOT_TOKEN не задан — просто пишем варнинг и не запускаем бота.
    """
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            logger.warning(
                "TELEGRAM_BOT_TOKEN не задан; Telegram-бот не будет запущен."
            )
            return

        from . import telegram_bot

        logger.info("Запускаю Telegram-бота в фонового потоке...")
        t = threading.Thread(
            target=telegram_bot.main,
            name="telegram-bot-thread",
            daemon=True,
        )
        t.start()
    except Exception:
        logger.exception("Не удалось запустить Telegram-бота в фоне")


# ВАЖНО: вызываем после определения всего приложения
_start_bot_background()
