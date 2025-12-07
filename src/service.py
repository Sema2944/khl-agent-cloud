# src/service.py
from __future__ import annotations

import logging
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

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
from .hockey_logic import khl_today_text_from_winline, build_match_context_notes
from .khl_form_client import get_team_form, TeamForm, TeamAdvancedForm
from .winline_client import get_khl_events_today

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# FASTAPI APP
# ------------------------------------------------------------
app = FastAPI(title="KHL AI Betting Agent API")


# ------------------------------------------------------------
# HEALTHCHECK
# ------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


# ------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------

class QueryRequest(BaseModel):
    user_id: int
    message: str


class QueryResponse(BaseModel):
    reply: str


class BetCreate(BaseModel):
    user_id: int
    raw_text: str | None = None
    stake: float | None = None
    odds: float | None = None
    event: str | None = None
    outcome: str | None = None


class BetSettle(BaseModel):
    user_id: int
    bet_id: int
    result: str


# ------------------------------------------------------------
# Startup
# ------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("FastAPI service started (Bot works in separate worker).")


# ------------------------------------------------------------
# Agent Query
# ------------------------------------------------------------

@app.post("/agent/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    logger.info("/agent/query: user_id=%s, message=%r", req.user_id, req.message)

    try:
        from .parsing import run_dialog_agent
    except Exception as e:
        logger.exception("Import error for run_dialog_agent")
        return QueryResponse(
            reply=f"⚠️ Ошибка сервера (import): {type(e).__name__}: {e}"
        )

    try:
        reply = await run_dialog_agent(user_id=req.user_id, message=req.message)
    except Exception as e:
        logger.exception("run_dialog_agent failed")
        return QueryResponse(
            reply=f"⚠️ Ошибка агента: {type(e).__name__}: {e}"
        )

    return QueryResponse(reply=reply)


# ------------------------------------------------------------
# BETS API
# ------------------------------------------------------------

@app.get("/agent/last-bets")
def api_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    bets = get_last_bets(session, user_id, limit)
    # Приводим SQLModel объекты к dict
    return {"bets": [b.dict() for b in bets]}


@app.post("/agent/add-bet")
def api_add_bet(
    data: BetCreate,
    session: Session = Depends(get_session),
):
    bet = add_bet(
        session=session,
        user_id=data.user_id,
        raw_text=data.raw_text or data.event or "",
        stake=data.stake,
        odds=data.odds,
        event=data.event,
        outcome=data.outcome,
    )
    return {"bet_id": bet.id}


@app.post("/agent/settle-bet")
def api_settle_bet(
    data: BetSettle,
    session: Session = Depends(get_session),
):
    bet = settle_bet(session, data.user_id, data.bet_id, data.result)
    if bet is None:
        return {"status": "error", "detail": "Bet not found"}
    return {"status": "ok"}


@app.get("/agent/stats")
def api_user_stats(
    user_id: int,
    session: Session = Depends(get_session),
):
    stats = get_user_stats(session, user_id)
    return stats.__dict__


@app.get("/agent/bank")
def api_user_bank(
    user_id: int,
    session: Session = Depends(get_session),
):
    bank = get_user_bank(session, user_id)
    return {"bank": bank}


@app.post("/agent/bank/set")
def api_set_user_bank_api(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    user = set_user_bank(session, user_id, amount)
    return {"bank": user.bank}


@app.post("/agent/bank/change")
def api_change_user_bank_api(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    user = change_user_bank(session, user_id, amount)
    return {"bank": user.bank}


@app.get("/agent/all-bets")
def api_all_bets(
    user_id: int,
    session: Session = Depends(get_session),
):
    bets = get_all_bets(session, user_id)
    return {"bets": [b.dict() for b in bets]}


# ------------------------------------------------------------
# KHL API
# ------------------------------------------------------------

@app.get("/khl/today")
async def api_khl_today():
    text = await khl_today_text_from_winline()
    return {"text": text}


@app.get("/khl/match-context/{event_id}")
async def api_match_context(event_id: int):
    events = await get_khl_events_today()
    event = next((e for e in events if e["id"] == event_id), None)

    if not event:
        return {"error": "Match not found"}

    ctx = await build_match_context_notes(event)
    return ctx


@app.get("/khl/team-form/{team}")
async def api_team_form(team: str):
    form: TeamForm | TeamAdvancedForm = await get_team_form(team)
    return form.dict()
