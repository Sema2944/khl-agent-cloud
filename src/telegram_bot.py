# src/telegram_bot.py
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from .db import async_session, Bet  # твои модели/сессия из src/db.py

log = logging.getLogger("svc")


# =============== Бизнес-логика =================
async def _add_bet_to_db(text: str) -> int:
    async with async_session() as session:
        bet = Bet(text=text)
        session.add(bet)
        await session.commit()
        await session.refresh(bet)
        return bet.id

async def _get_bets_from_db(limit: int = 50) -> list[Bet]:
    async with async_session() as session:
        res = await session.execute(
            Bet.__table__.select().order_by(Bet.id.desc()).limit(limit)
        )
        rows = res.fetchall()
        # rows -> list[Row]; приведём к Bet-подобным объектам с атрибутами
        return [Bet(id=r.id, text=r.text, created_at=r.created_at) for r in rows]  # type: ignore

async def _clear_bets_if_pin_ok(user_pin: str) -> bool:
    needed = os.getenv("ADMIN_PIN", "100182")
    if user_pin != needed:
        return False
    async with async_session() as session:
        await session.execute(Bet.__table__.delete())
        await session.commit()
    return True


# =============== Команды бота =================
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "Привет! Я бот.\n"
        "Команды:\n"
        "<b>/addbet &lt;текст&gt;</b> — добавить ставку\n"
        "<b>/bets</b> — показать последние ставки\n"
        "<b>/clearbets &lt;PIN&gt;</b> — очистить ставки (для админа)\n"
        "<b>/help</b> — помощь",
    )

async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "Доступные команды:\n"
        "• /addbet <i>текст</i>\n"
        "• /bets\n"
        "• /clearbets <i>PIN</i>",
    )

async def _cmd_addbet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args_text = " ".join(context.args).strip() if context.args else ""
    if not args_text:
        await update.effective_chat.send_message(
            "Укажи текст: <code>/addbet Тестовая ставка</code>"
        )
        return
    bet_id = await _add_bet_to_db(args_text)
    await update.effective_chat.send_message(f"✅ Ставка добавлена: #{bet_id}")

async def _cmd_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bets = await _get_bets_from_db(50)
    if not bets:
        await update.effective_chat.send_message("Пока ставок нет.")
        return
    lines = [f"#{b.id}: {b.text}" for b in bets[::-1]]  # старые сверху
    await update.effective_chat.send_message("<b>Ставки:</b>\n" + "\n".join(lines))

async def _cmd_clearbets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pin = " ".join(context.args).strip() if context.args else ""
    ok = await _clear_bets_if_pin_ok(pin)
    if not ok:
        await update.effective_chat.send_message("❌ Неверный PIN.")
        return
    await update.effective_chat.send_message("🧹 Ставки очищены.")


# =============== Периодические задачи =================
async def _job_refresh_line(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Здесь парсинг линии букмекера, обновление кэша и т.п.
    # Пока просто логируем, чтобы не падало.
    log.info("[JOB] Обновление линии…")


# =============== Построение Application =================
async def build_bot_app() -> Application:
    # Defaults: сразу включаем HTML
    defaults = Defaults(parse_mode=ParseMode.HTML)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)  # безопасней для веб-приложений
        .defaults(defaults)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("addbet", _cmd_addbet))
    app.add_handler(CommandHandler("bets", _cmd_bets))
    app.add_handler(CommandHandler("clearbets", _cmd_clearbets))

    # Планировщик: безопасно работаем без extra, если не установлен
    jq = app.job_queue
    if jq is None:
        log.warning(
            "[BOT] JobQueue недоступен. Установи extra: python-telegram-bot[job-queue]"
        )
    else:
        jq.run_repeating(_job_refresh_line, interval=300, first=5)

    return app


# =============== Локальный запуск (опционально) =================
async def _main() -> None:
    app = await build_bot_app()
    # polling внутри отдельной задачи — как на сервере
    await app.initialize()
    try:
        await app.start()
        await app.updater.start_polling()  # type: ignore[attr-defined]
        log.info("[BOT] Polling запущен.")
        # держим процесс
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()  # type: ignore[attr-defined]
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())




