from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
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

from src.khl_client import BetLine, get_today_lines

logger = logging.getLogger(__name__)

# ====== SQLite: путь к файлу ======
DB_PATH = Path(os.getenv("BETBOT_DB_PATH", "betbot.sqlite3"))

# Глобальный объект приложения бота
_bot_app: Optional[Application] = None
_bot_started: bool = False  # флаг, чтобы не стартовать несколько раз


# ====================== SQLite-хелперы ======================

def _ensure_db() -> None:
    """Создаём таблицы, если их ещё нет."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                first_name TEXT,
                username   TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _register_user_from_update(update: Update) -> None:
    """Сохраняем пользователя, если ещё не сохранён."""
    if update.message is None:
        return
    user = update.message.from_user
    if user is None:
        return

    chat_id = update.message.chat_id
    first_name = user.first_name or ""
    username = user.username or ""
    created_at = update.message.date.isoformat() if update.message.date else ""

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (chat_id, first_name, username, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, first_name, username, created_at),
        )
        conn.commit()


def _save_bet(chat_id: int, text: str, created_at_iso: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO bets (chat_id, text, created_at) VALUES (?, ?, ?)",
            (chat_id, text, created_at_iso),
        )
        conn.commit()


def _clear_bets(chat_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM bets WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()
        return cur.rowcount


def _get_all_chat_ids() -> List[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT chat_id FROM users").fetchall()
    return [r[0] for r in rows]


# ====================== Формататоры ======================

def _format_edge(edge: Optional[float]) -> str:
    if edge is None:
        return "-"
    sign = "+" if edge > 0 else ""
    return f"{sign}{round(edge * 100, 1)}%"


def _format_prob(prob: Optional[float]) -> str:
    if prob is None:
        return "-"
    return f"{round(prob * 100, 1)}%"


def _format_lines_for_message(lines: List[BetLine], title: str) -> str:
    chunks: list[str] = [title]

    for line in lines[:5]:
        try:
            start_str = line.start.strftime("%d.%m %H:%M")

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
                reco_line = (
                    f"Рекомендация модели: {best_side} "
                    f"(value {round(best_edge_val * 100, 1)}%)"
                )
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

    return "\n\n".join(chunks)


# ====================== HANDLERS ======================

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    _register_user_from_update(update)

    text = (
        "Привет! Я бот учёта ставок.\n\n"
        "Доступные команды:\n"
        "/addbet Описание ставки — добавить ставку\n"
        "/clearbets — очистить список своих ставок\n"
        "/bets — показать актуальные линии и рекомендации\n"
        "/help — показать справку\n\n"
        "⚠️ Помни, что ставки — это риск. Не ставь больше, чем готов проиграть."
    )

    await update.message.reply_text(text)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    text = (
        "Справка по боту:\n\n"
        "/start — запустить бота и показать приветствие\n"
        "/addbet Описание ставки — добавить ставку в базу\n"
        "/clearbets — удалить все свои сохранённые ставки\n"
        "/bets — показать актуальные линии и рекомендации\n"
    )

    await update.message.reply_text(text)


async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    _register_user_from_update(update)

    if not context.args:
        await update.message.reply_text("Использование: /addbet Описание ставки")
        return

    description = " ".join(context.args)
    created_at_iso = (
        update.message.date.isoformat() if update.message.date else ""
    )

    _save_bet(update.message.chat_id, description, created_at_iso)

    await update.message.reply_text(f"Ставка сохранена: {description}")


async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    _register_user_from_update(update)

    deleted = _clear_bets(update.message.chat_id)
    if deleted:
        await update.message.reply_text(f"Удалено ставок: {deleted}.")
    else:
        await update.message.reply_text("У тебя не было сохранённых ставок.")


async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    _register_user_from_update(update)

    try:
        lines = await get_today_lines()
    except Exception as e:
        logger.exception("Ошибка при get_today_lines: %s", e)
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

    text = _format_lines_for_message(lines, "Актуальные линии на сегодня:\n")
    await update.message.reply_text(text)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    _register_user_from_update(update)

    await update.message.reply_text(
        "Я пока понимаю только команды.\n"
        "Напиши /help, чтобы посмотреть список доступных команд."
    )


# ====================== АВТОУВЕДОМЛЕНИЯ ======================

async def _notify_all_users_once() -> None:
    """
    Разослать всем пользователям актуальные линии (если есть).
    Используется в фоне.
    """
    global _bot_app

    if _bot_app is None:
        return

    try:
        lines = await get_today_lines()
    except Exception as e:
        logger.exception("Ошибка при get_today_lines в автоуведомлении: %s", e)
        return

    if not lines:
        logger.info("Автоуведомление: линий нет, рассылку пропускаем.")
        return

    text = _format_lines_for_message(
        lines,
        "Автообновление линий:\n",
    )

    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        logger.info("Автоуведомление: подписчиков нет, рассылку пропускаем.")
        return

    logger.info("Автоуведомление: рассылаем %d пользователям", len(chat_ids))

    for chat_id in chat_ids:
        try:
            await _bot_app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.exception(
                "Не удалось отправить автоуведомление в chat_id=%s: %s",
                chat_id,
                e,
            )


async def _auto_notify_loop() -> None:
    """
    Фоновая задача: периодически шлёт автоуведомления.
    Сейчас — раз в 3 часа.
    """
    # Небольшая пауза после старта, чтобы всё успело проинициализироваться
    await asyncio.sleep(30)

    while True:
        try:
            await _notify_all_users_once()
        except Exception as e:
            logger.exception("Ошибка в автоуведомлении: %s", e)

        # Ждём 3 часа до следующей рассылки
        await asyncio.sleep(3 * 60 * 60)


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

    # Инициализируем SQLite
    _ensure_db()

    if _bot_app is None:
        _bot_app = _create_application(token)
        logger.info("[BOT] Application создан.")

    return _bot_app


# ====================== ЗАПУСК / ОСТАНОВКА ======================

async def start_bot_polling() -> None:
    """
    Стартуем бота внутри того же event loop, что и FastAPI/uvicorn.
    Без run_polling, без потоков — только initialize/start/updater.start_polling.
    Плюс запускаем фоновую задачу автоуведомлений.
    """
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
        return

    # Запускаем фоновую задачу автоуведомлений
    loop = asyncio.get_running_loop()
    loop.create_task(_auto_notify_loop())


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
