# src/telegram_webhook.py
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request, Header, HTTPException
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

router = APIRouter()

# сюда положим Application при старте FastAPI
telegram_app: Application | None = None


def set_telegram_app(app: Application) -> None:
    global telegram_app
    telegram_app = app


@router.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram app not initialized")

    if SECRET:
        if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret token")

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)

    # ключевое: прокидываем апдейт в PTB Application
    await telegram_app.process_update(update)

    return {"ok": True}
