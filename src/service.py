# src/service.py
from __future__ import annotations

import os
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Depends, Request, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session
from telegram import Update

from .db import init_db, get_session
from .bets_db import (
    get_user_stats,
    add_bet,
    get_last_bets,
    settle_bet,
    get_user_bank,
    set_user_bank,
    change_user_bank,
    get_all_bets,
)

# --- Telegram webhook ---
from .telegram_bot.app import build_telegram_application
from telegram.ext import Application

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="KHL AI Betting Agent API")

# -----------------------------
# Healthcheck (Render)
# -----------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


@app.get("/__health")
def health():
    return {"ok": True}


@app.get("/favicon.ico")
def favicon():
    return {}


# -----------------------------
# Pydantic модели
# -----------------------------
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


# -----------------------------
# Telegram webhook state
# -----------------------------
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

telegram_app: Application | None = None


# -----------------------------
# Startup
# -----------------------------
@app.on_event("startup")
async def on_startup():
    """
    • инициализация БД
    • запуск Telegram Application
    • регистрация webhook
    """
    global telegram_app

    init_db()
    logger.info("DB initialized")

    # --- Telegram ---
    telegram_app = build_telegram_application()
    await telegram_app.initialize()
    await telegram_app.start()

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")

    if not token or not public_base:
        logger.warning("Telegram webhook NOT set (missing token or PUBLIC_BASE_URL)")
        return

    webhook_url = f"{public_base}{TELEGRAM_WEBHOOK_PATH}"

    async with httpx.AsyncClient(timeout=5.0) as client:
        payload = {"url": webhook_url}
        if TELEGRAM_WEBHOOK_SECRET:
            payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET

        r = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json=payload,
        )
        r.raise_for_status()

    logger.info("Telegram webhook registered: %s", webhook_url)


# -----------------------------
# Telegram webhook endpoint
# -----------------------------
@app.post(TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram app not ready")

    if TELEGRAM_WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)

    await telegram_app.process_update(update)
    return {"ok": True}


# -----------------------------
# Agent endpoint
# -----------------------------
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
        return QueryResponse(
            reply=f"⚠️ Ошибка сервера (импорт агента): {type(e).__name__}: {e}"
        )

    try:
        reply = await run_dialog_agent(user_id=req.user_id, message=text)
    except Exception as e:
        logger.exception("Ошибка внутри run_dialog_agent")
        return QueryResponse(
            reply=f"⚠️ Внутренняя ошибка агента: {type(e).__name__}: {e}"
        )

    return QueryResponse(reply=reply)


# -----------------------------
# Bets API
# -----------------------------
@app.get("/agent/last-bets")
def api_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    bets = get_last_bets(session, user_id, limit)
    return {"bets": [b.model_dump() for b in bets]}


@app.post("/agent/add-bet")
def api_add_bet(
    bet: BetCreate,
    session: Session = Depends(get_session),
):
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
def api_settle_bet(
    data: BetSettle,
    session: Session = Depends(get_session),
):
    b = settle_bet(session, data.user_id, data.bet_id, data.result)
    if b is None:
        return {"status": "not_found_or_bad_result"}
    return {"status": "ok", "bet": b.model_dump()}


@app.get("/agent/stats")
def api_user_stats(
    user_id: int,
    session: Session = Depends(get_session),
):
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
def api_user_bank(
    user_id: int,
    session: Session = Depends(get_session),
):
    return {"bank": get_user_bank(session, user_id)}


@app.post("/agent/bank/set")
def api_set_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    u = set_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": u.bank}


@app.post("/agent/bank/change")
def api_change_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    u = change_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": u.bank}


@app.get("/agent/all-bets")
def api_all_bets(
    user_id: int,
    session: Session = Depends(get_session),
):
    bets = get_all_bets(session, user_id)
    return {"bets": [b.model_dump() for b in bets]}
