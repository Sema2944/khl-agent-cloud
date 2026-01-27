# src/telegram_bot/app.py
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .autochehol import handle_autochehol_callback, handle_autochehol_message, start_autochehol

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip()
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = (os.getenv("TELEGRAM_WEBHOOK_URL") or "").strip()

_telegram_app: Optional[Application] = None


def _build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start telegram bot")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "autochehol"], start_autochehol))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    return app


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handled = await handle_autochehol_callback(update, context)
    if not handled and update.callback_query:
        await update.callback_query.answer()


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handled = await handle_autochehol_message(update, context)
    if not handled and update.message:
        await update.message.reply_text("Напишите /start, чтобы открыть меню бота.")


async def _ensure_application() -> Application:
    global _telegram_app
    if _telegram_app is None:
        _telegram_app = _build_application()
    return _telegram_app


def mount_telegram_routes(app: FastAPI) -> None:
    router = APIRouter()

    @router.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> dict[str, str]:
        telegram_app = await _ensure_application()
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": "true"}

    app.include_router(router)


async def telegram_startup() -> None:
    telegram_app = await _ensure_application()
    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = WEBHOOK_URL or (f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else "")
    if webhook_url:
        await telegram_app.bot.set_webhook(url=webhook_url)
        logger.info("Webhook set to %s", webhook_url)
    else:
        logger.warning("PUBLIC_URL/TELEGRAM_WEBHOOK_URL not set -> webhook not configured.")


async def telegram_shutdown() -> None:
    telegram_app = await _ensure_application()
    await telegram_app.stop()
    await telegram_app.shutdown()
