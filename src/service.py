# src/service.py

import logging
import threading

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session
from .bets_db import get_user_stats
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
    text = (message or "").lower().strip()

    # 1) Показать статистику по ставкам
    if "статист" in text or "статку" in text or "stats" in text:
        stats = get_user_stats(session, user_id)
        if stats.total_bets == 0:
            return "Пока нет ни одной сохранённой ставки. Начнём с первой 😉"

        return (
            "Твоя статистика:\n"
            f"Ставок: {stats.total_bets}\n"
            f"Винрейт: {stats.winrate:.1f}%\n"
            f"ROI: {stats.roi:.2f}%\n"
            f"Плюс/минус: {stats.pnl:.0f} ₽"
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

    # 3) Заглушка под добавление ставки
    if text.startswith("ставка ") or text.startswith("поставь "):
        return (
            "Я уже понимаю, что ты хочешь сделать ставку,\n"
            "но пока не сохраняю её в базу.\n"
            "Дальше можно будет добавить парсинг и запись в БД 💾"
        )

    # 4) Ответ по умолчанию
    return (
        "Я AI-агент для ставок.\n"
        "Сейчас умею:\n"
        "• По словам 'статистика / статку' показывать твою статистику\n"
        "• По запросу 'КХЛ сегодня' показывать матчи КХЛ (через линии бука)\n\n"
        "Попробуй: 'Покажи мою статистику' или 'Какие матчи КХЛ сегодня?'."
    )


# ------------------ ЗАПУСК TELEGRAM-БОТА В ФОНЕ ------------------


def _start_bot_background() -> None:
    """
    Стартуем Telegram-бота в отдельном потоке,
    чтобы он жил внутри того же процесса, что и FastAPI.
    Это позволяет использовать бесплатный Web Service на Render.
    """
    try:
        # импортируем здесь, чтобы избежать циклических импортов
        from . import telegram_bot

        logger.info("Запускаю Telegram-бота в фонового потоке...")
        # отдельный поток, чтобы не блокировать uvicorn
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
