# src/telegram_bot.py
from __future__ import annotations

import logging
import os
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

from src.khl_client import BetLine, get_today_lines  # <-- ВАЖНО: так

logger = logging.getLogger(__name__)

_bot_app: Optional[Application] = None
_bot_started: bool = False


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

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

    await update.message.reply_text("Все ставки очищены (заглушка).")


def _format_edge(edge: Optional[float]) -> str:
    if edge is None:
        return "-"
    sign = "+" if edge > 0 else ""
    return f"{sign}{round(edge * 100, 1)}%"


def _format_prob(prob: Optional[float]) -> str:
    if prob is None:
        return "-"
    return f"{round(prob * 100, 1)}%"


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        lines: List[BetLine] = await get_today_lines()
    except Exception as e:
        logger.exception("Ошибка при вызове get_today_lines: %s", e)
        await update.message.reply_text(
            "Не удалось получить актуальные линии — внутренняя ошибка.\n"
            "Попробуй ещё раз чуть позже."
        )
        return

    if not lines:
        await update.message.reply_text(
            "На сегодня нет доступных линий.\n"
            "Как только будут матчи, я смогу показать коэффициенты и рекомендации."
        )
        return

    chunks: list[str] = ["Актуальные линии на сегодня:\n"]

    for line in lines[:5]:
        try:
            start_str = line.start.strftime("%d.%m %H:%M UTC")
            odds_draw_str = "-" if line.odds_draw is None else str(line.odds_draw)

            prob_home_str = _format_prob(line.model_prob_home)
            prob_draw_str = _format_prob(line.model_prob_draw)
            prob_away_str = _format_prob(line.model_prob_away)

            edge_home_str = _format_edge(line.edge_home)
            edge_draw_str = _format_edge(line.edge_draw)
            edge_away_str = _format_edge(line.edge_away)

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


def _create_application(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))
    app.add_handler(CommandHandler("bets", _cmd_bets))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    return app


async def build_bot_app() -> Optional[Application]:
    global _bot_app

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.warning("⚠️ TELEGRAM_TOKEN не задан — бот не будет запущен.")
        return None

    if _bot_app is None:
        _bot_app = _create_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


async def start_bot_polling() -> None:
    global _bot_app, _bot_started

    if _bot_app is None:
        logger.warning("[BOT] start_bot_polling вызван, но _bot_app is None.")
        return

    if _bot_started:
        logger.info("[BOT] Уже запущен, пропускаем повторный start.")
        return

    await _bot_app.initialize()
    await _bot_app.start()

    try:
        await _bot_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.exception("[BOT] Не удалось удалить webhook: %s", e)

    if getattr(_bot_app, "updater", None) is not None:
        await _bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("[BOT] Polling запущен.")
        _bot_started = True
    else:
        logger.warning("[BOT] У Application нет updater — polling не запущен.")


async def stop_bot_polling() -> None:
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
