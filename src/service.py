# src/service.py
import logging
import os

from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Autochehol Bot API")


@app.get("/__health")
def health():
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "autochehol-bot"}


# ---------------------------------------------------------------------
# Telegram wiring (routes mount at import-time, startup sets webhook)
# ---------------------------------------------------------------------
_TELEGRAM_MOUNTED = False


def _maybe_mount_telegram_routes() -> None:
    """
    Монтируем роуты Telegram в FastAPI (POST /telegram/webhook),
    но только если есть токен. Это важно: иначе сервис не должен падать.
    """
    global _TELEGRAM_MOUNTED
    if _TELEGRAM_MOUNTED:
        return

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN missing -> telegram routes NOT mounted.")
        return

    try:
        from .telegram_bot.app import mount_telegram_routes

        mount_telegram_routes(app)
        _TELEGRAM_MOUNTED = True
        logger.info("Telegram routes mounted.")
    except Exception as exc:
        logger.exception("Failed to mount telegram routes: %s", exc)


# монтируем роуты сразу (до старта), чтобы FastAPI знал /telegram/webhook
_maybe_mount_telegram_routes()


@app.on_event("startup")
async def on_startup():
    # запускаем PTB + ставим webhook (если токен/URL есть)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN missing -> telegram startup skipped.")
        return

    try:
        from .telegram_bot.app import telegram_startup

        await telegram_startup()
        logger.info("Telegram startup complete.")
    except Exception as exc:
        logger.exception("Telegram startup failed: %s", exc)


@app.on_event("shutdown")
async def on_shutdown():
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return

    try:
        from .telegram_bot.app import telegram_shutdown

        await telegram_shutdown()
        logger.info("Telegram shutdown complete.")
    except Exception as exc:
        logger.exception("Telegram shutdown failed: %s", exc)
