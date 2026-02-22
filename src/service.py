# src/service.py
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session
from .bets_db import (
    add_bet,
    change_user_bank,
    get_all_bets,
    get_last_bets,
    get_user_bank,
    get_user_stats,
    set_user_bank,
    settle_bet,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="KHL AI Betting Agent API")


@app.get("/__health")
def health():
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


@app.get("/favicon.ico")
def favicon():
    return {}


class QueryRequest(BaseModel):
    user_id: int
    message: Optional[str] = None
    query: Optional[str] = None

    @property
    def text(self) -> str:
        return (self.message or self.query or "").strip()


class QueryResponse(BaseModel):
    reply: str


class BetCreate(BaseModel):
    user_id: int
    raw_text: str
    stake: Optional[float] = None
    odds: Optional[float] = None
    event: Optional[str] = None
    outcome: Optional[str] = None


class BetSettle(BaseModel):
    user_id: int
    bet_id: int
    result: str  # "win", "lose", "push"


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
    except Exception as e:
        logger.exception("Failed to mount telegram routes: %s", e)


# монтируем роуты сразу (до старта), чтобы FastAPI знал /telegram/webhook
_maybe_mount_telegram_routes()


async def _hunter_loop():
    """Background loop: runs daily Hunter at 08:00 UTC."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    # Wait 30 seconds for the app to fully start
    await asyncio.sleep(30)

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Check if today's picks exist; if not and it's past 08:00 — run now
            run_now = False
            if now.hour >= 8:
                try:
                    from .daily_pro import _fetch_all_matches_today
                    from sqlmodel import Session as SMSession
                    from sqlalchemy import text
                    from .db import engine as db_engine
                    from zoneinfo import ZoneInfo
                    today_msk = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
                    with SMSession(db_engine) as s:
                        count = s.exec(
                            text("SELECT COUNT(*) FROM daily_picks WHERE pick_date = :d"),
                            params={"d": today_msk},
                        ).scalar()
                        if not count:
                            run_now = True
                except Exception:
                    logger.exception("Hunter loop: check for existing picks failed")

            if run_now:
                logger.info("Hunter loop: no picks for today, running now")
                try:
                    from .daily_pro import run_daily_hunter
                    from .telegram_bot.app import _telegram_app
                    bot = _telegram_app.bot if _telegram_app else None
                    await run_daily_hunter(bot=bot)
                except Exception:
                    logger.exception("Hunter loop: immediate run failed")

            # Calculate sleep until next 08:00 UTC
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info("Hunter loop: sleeping %.0f seconds until next 08:00 UTC", wait)
            await asyncio.sleep(wait)

            # Run daily hunter
            try:
                from .daily_pro import run_daily_hunter
                from .telegram_bot.app import _telegram_app
                bot = _telegram_app.bot if _telegram_app else None
                await run_daily_hunter(bot=bot)
            except Exception:
                logger.exception("Hunter loop: scheduled run failed")

        except asyncio.CancelledError:
            logger.info("Hunter loop cancelled")
            break
        except Exception:
            logger.exception("Hunter loop: unexpected error, retrying in 60s")
            await asyncio.sleep(60)


async def _expiry_loop():
    """Background loop: checks expired subscriptions every hour."""
    import asyncio

    # Wait 60 seconds for app to start
    await asyncio.sleep(60)

    while True:
        try:
            from .telegram_bot.payments import check_expired_subscriptions
            from .telegram_bot.app import _telegram_app
            bot = _telegram_app.bot if _telegram_app else None
            await check_expired_subscriptions(bot)
        except asyncio.CancelledError:
            logger.info("Expiry loop cancelled")
            break
        except Exception:
            logger.exception("Expiry loop: error")

        # Sleep 1 hour
        await asyncio.sleep(3600)


@app.on_event("startup")
async def on_startup():
    init_db()
    logger.info("FastAPI startup: DB initialized.")

    # запускаем PTB + ставим webhook (если токен/URL есть)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN missing -> telegram startup skipped.")
        return

    try:
        from .telegram_bot.app import telegram_startup

        await telegram_startup()
        logger.info("Telegram startup complete.")
    except Exception as e:
        logger.exception("Telegram startup failed: %s", e)

    # Send startup notification to admin
    try:
        from .alerting import send_startup_message
        from .telegram_bot.app import _telegram_app
        if _telegram_app:
            await send_startup_message(_telegram_app.bot)
    except Exception:
        logger.exception("Failed to send startup message")

    # Hunter scheduler (asyncio background task)
    try:
        import asyncio
        asyncio.create_task(_hunter_loop())
        logger.info("Hunter scheduler started (daily at 08:00 UTC)")
    except Exception:
        logger.exception("Hunter scheduler failed to start")

    # Subscription expiry checker (every hour)
    try:
        import asyncio
        asyncio.create_task(_expiry_loop())
        logger.info("Subscription expiry checker started (hourly)")
    except Exception:
        logger.exception("Expiry checker failed to start")


@app.on_event("shutdown")
async def on_shutdown():
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return

    try:
        from .telegram_bot.app import telegram_shutdown

        await telegram_shutdown()
        logger.info("Telegram shutdown complete.")
    except Exception as e:
        logger.exception("Telegram shutdown failed: %s", e)


@app.post("/agent/query", response_model=QueryResponse)
async def agent_query(req: QueryRequest):
    text = req.text
    logger.info("/agent/query: user_id=%s, text=%r", req.user_id, text)

    if not text:
        return QueryResponse(reply="Пустой запрос 😕")

    try:
        from .parsing import run_dialog_agent
    except Exception as e:
        logger.exception("Не удалось импортировать run_dialog_agent")
        return QueryResponse(reply=f"⚠️ Ошибка сервера (импорт агента): {type(e).__name__}: {e}")

    try:
        reply = await run_dialog_agent(user_id=req.user_id, text=text)
    except Exception as e:
        logger.exception("Ошибка внутри run_dialog_agent")
        return QueryResponse(reply=f"⚠️ Внутренняя ошибка агента: {type(e).__name__}: {e}")

    return QueryResponse(reply=reply)


@app.get("/agent/last-bets")
def api_last_bets(user_id: int, limit: int = 5, session: Session = Depends(get_session)):
    bets = get_last_bets(session, user_id, limit)
    return {"bets": [b.model_dump() for b in bets]}


@app.post("/agent/add-bet")
def api_add_bet(bet: BetCreate, session: Session = Depends(get_session)):
    b = add_bet(
        session=session,
        user_id=bet.user_id,
        raw_text=bet.raw_text,
        stake=bet.stake,
        odds=bet.odds,
        event=bet.event,
        outcome=bet.outcome,
    )
    return {"bet_id": b.id}


@app.post("/agent/settle-bet")
def api_settle_bet(data: BetSettle, session: Session = Depends(get_session)):
    b = settle_bet(session, data.user_id, data.bet_id, data.result)
    if b is None:
        return {"status": "not_found_or_bad_result"}
    return {"status": "ok", "bet": b.model_dump()}


@app.get("/agent/stats")
def api_user_stats(user_id: int, session: Session = Depends(get_session)):
    s = get_user_stats(session, user_id)
    return {
        "total_bets": s.total_bets,
        "settled_bets": s.settled_bets,
        "pushes": s.pushes,
        "winrate": s.winrate,
        "roi": s.roi,
        "pnl": s.pnl,
        "total_stake": s.total_stake,
    }


@app.get("/agent/bank")
def api_user_bank(user_id: int, session: Session = Depends(get_session)):
    return {"bank": get_user_bank(session, user_id)}


@app.post("/agent/bank/set")
def api_set_user_bank(user_id: int, amount: float, session: Session = Depends(get_session)):
    u = set_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": u.bank}


@app.post("/agent/bank/change")
def api_change_user_bank(user_id: int, amount: float, session: Session = Depends(get_session)):
    u = change_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": u.bank}


@app.get("/agent/all-bets")
def api_all_bets(user_id: int, session: Session = Depends(get_session)):
    bets = get_all_bets(session, user_id)
    return {"bets": [b.model_dump() for b in bets]}
