from __future__ import annotations

import logging
import os
from datetime import timezone
from typing import Optional, List

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import khl_client

logger = logging.getLogger(__name__)

# Глобальный объект приложения бота
_bot_app: Optional[Application] = None


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # ВАЖНО: только обычный текст, без HTML-тегов (<b>, <i> и т.п.)
    text = (
        "Привет! Я бот учёта ставок.\n\n"
        "Доступные команды:\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — очистить список ставок\n"
        "/bets — показать актуальные линии и рекомендации\n"
        "/help — показать справку\n"
    )

    await update.message.reply_text(text)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    text = (
        "Справка по боту:\n\n"
        "/start — запустить бота и показать приветствие\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — удалить все сохранённые ставки\n"
        "/bets — показать актуальные линии и рекомендации\n"
    )

    await update.message.reply_text(text)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not context.args:
        await update.message.reply_text("Использование: /addbet Описание ставки")
        return

    description = " ".join(context.args)
    await update.message.reply_text(f"Ставка добавлена: {description}")


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    # Здесь позже можно будет вызвать очистку из БД
    await update.message.reply_text("Все ставки очищены (заглушка).")


def _format_edge(edge: Optional[float]) -> str:
    if edge is None:
        return "-"
    # edge=0.04 -> "+4%"
    sign = "+" if edge > 0 else ""
    return f"{sign}{round(edge * 100, 1)}%"


def _format_prob(prob: Optional[float]) -> str:
    if prob is None:
        return "-"
    return f"{round(prob * 100, 1)}%"


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает пользователю актуальные линии на сегодня на основе khl_client.get_today_lines().
    Использует твою датакласс-модель BetLine.
    """
    if update.message is None:
        return

    try:
        lines: List[khl_client.BetLine] = await khl_client.get_today_lines()
    except Exception as e:
        logger.exception("Ошибка при вызове khl_client.get_today_lines: %s", e)
        await update.message.reply_text(
            "Не удалось получить актуальные линии — внутреняя ошибка.\n"
            "Попробуй ещё раз чуть позже."
        )
        return

    if not lines:
        await update.message.reply_text(
            "На сегодня нет доступных линий (khl_client вернул пустой список).\n"
            "Как только будут матчи, я смогу показать коэффициенты и рекомендации."
        )
        return

    chunks: list[str] = ["Актуальные линии на сегодня:\n"]

    # Ограничимся, например, 5 матчами, чтобы не спамить
    for line in lines[:5]:
        try:
            start_str = line.start.strftime("%d.%m %H:%M UTC")

            # Коэффициенты
            odds_draw_str = "-" if line.odds_draw is None else str(line.odds_draw)

            # Вероятности модели
            prob_home_str = _format_prob(line.model_prob_home)
            prob_draw_str = _format_prob(line.model_prob_draw)
            prob_away_str = _format_prob(line.model_prob_away)

            # Эдж (value)
            edge_home_str = _format_edge(line.edge_home)
            edge_draw_str = _format_edge(line.edge_draw)
            edge_away_str = _format_edge(line.edge_away)

            # Простая рекомендация: ищем наибольший положительный edge
            best_side = None
            best_edge_val = 0.0

            for side, edge_val in [
                ("дом", line.edge_home),
                ("ничья", line.edge_draw),
                ("гости", line.edge_away),
            ]:
                if edge_val is not None and edge_val > best_edge_val:
                    best_edge_val = edge_val
                    best_side = side

            if best_side and best_edge_val > 0:
                reco_line = f"Рекомендация модели: {best_side} (value {round(best_edge_val * 100, 1)}%)"
            else:
                reco_line = "Рекомендация модели: явного value нет."

            block = (
                f"{line.league}: {line.home} — {line.away}\n"
                f"Время начала: {start_str}\n"
                f"Рынок: {line.market} | Букмекер: {line.bookmaker}\n"
                f"Коэффициенты: дом {line.odds_home}, ничья {odds_draw_str}, гости {line.odds_away}\n"
                f"Вероятности модели: дом {prob_home_str}, ничья {prob_draw_str}, гости {prob_away_str}\n"
                f"Эдж (value): дом {edge_home_str}, ничья {edge_draw_str}, гости {edge_away_str}\n"
                f"{reco_line}"
            )
            chunks.append(block)
        except Exception as e:
            logger.exception("Ошибка форматирования BetLine: %s", e)
            continue

    text = "\n\n".join(chunks)

    await update.message.reply_text(text)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Я пока понимаю только команды.\n"
        "Напиши /help, чтобы посмотреть список доступных команд."
    )


# ====================== ВСПОМОГАТЕЛЬНОЕ ======================

def _create_application(token: str) -> Application:
    """
    Создаём Application и регистрируем хендлеры.
    НИКАКОГО parse_mode здесь нет.
    """
    app = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))
    app.add_handler(CommandHandler("bets", _cmd_bets))

    # Любой текст без команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    return app


async def build_bot_app() -> Optional[Application]:
    """
    Создаёт и кэширует Application, если задан TELEGRAM_TOKEN.
    Ничего не запускает, только строит.
    """
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    if _bot_app is None:
        _bot_app = _create_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


# ====================== ЗАПУСК / ОСТАНОВКА (ASGI-style) ======================

_bot_started: bool = False  # наш флаг, чтобы не стартовать несколько раз


async def start_bot_polling() -> None:
    """
    Стартуем бота внутри того же event loop, что и FastAPI/uvicorn.
    Без run_polling, без потоков — только initialize/start/updater.start_polling.
    """
    global _bot_app, _bot_started

    if _bot_app is None:
        logger.warning("[BOT] start_bot_polling вызван, но _bot_app is None.")
        return

    if _bot_started:
        logger.info("[BOT] Уже запущен, пропускаем повторный start.")
        return

    # Инициализация Application
    await _bot_app.initialize()
    await _bot_app.start()

    # На всякий случай чистим webhook и сбрасываем старые апдейты
    try:
        await _bot_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.exception("[BOT] Не удалось удалить webhook: %s", e)

    # Запускаем polling через updater (PTB v21)
    if getattr(_bot_app, "updater", None) is not None:
        await _bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("[BOT] Polling запущен.")
        _bot_started = True
    else:
        logger.warning("[BOT] У Application нет updater — polling не запущен.")


async def stop_bot_polling() -> None:
    """
    Корректно останавливает бота при остановке приложения.
    """
    global _bot_app, _bot_started

    if _bot_app is None or not _bot_started:
        logger.info("[BOT] stop_bot_polling: бот уже остановлен или не запускался.")
        return

    if getattr(_bot_app, "updater", None) is not None:
        await _bot_app.updater.stop()

    await _bot_app.stop()
    await _bot_app.shutdown()

    _bot_started = False
    logger.info("[BOT] Остановлен.")








