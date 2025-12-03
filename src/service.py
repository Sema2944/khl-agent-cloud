# src/service.py
from __future__ import annotations

import logging
from typing import List

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session, User
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
from .khl_form_client import (
    get_team_form,
    TeamForm,
    TeamAdvancedForm,
)
from .winline_client import get_khl_events_today

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# FASTAPI ПРИЛОЖЕНИЕ
# ------------------------------------------------------------
app = FastAPI(title="KHL AI Betting Agent API")


# ------------------------------------------------------------
# HEALTH-CHECK ДЛЯ RENDER
# ------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


# ------------------------------------------------------------
# Pydantic-модели
# ------------------------------------------------------------

class QueryRequest(BaseModel):
    # ВАЖНО: поле называется message,
    # потому что телеграм-бот шлёт {"user_id": ..., "message": "..."}
    user_id: int
    message: str


class QueryResponse(BaseModel):
    reply: str


class BetCreate(BaseModel):
    user_id: int
    market: str
    odds: float
    stake: float


class BetSettle(BaseModel):
    user_id: int
    bet_id: int
    # ожидаем строки вида "win", "lose", "push"
    result: str


# ------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ БД
# ------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("FastAPI сервис запущен (бот работает в отдельном процессе).")


# ------------------------------------------------------------
# /agent/query — главный диалоговый эндпоинт
# ------------------------------------------------------------

@app.post("/agent/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """
    Запрос от Telegram-бота к LLM-агенту.

    Телеграм-бекенд шлёт JSON:
    {
        "user_id": 5027679117,
        "message": "профиль"
    }
    """
    from .parsing import run_dialog_agent

    reply = await run_dialog_agent(
        user_id=req.user_id,
        message=req.message,
    )
    return QueryResponse(reply=reply)


# ------------------------------------------------------------
# /agent/last-bets — последние ставки
# ------------------------------------------------------------

@app.get("/agent/last-bets")
def api_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    """
    Возвращает последние ставки пользователя.

    ВАЖНО: get_last_bets ожидает первым аргументом session,
    поэтому передаём именно (session, user_id, limit).
    """
    bets = get_last_bets(session, user_id, limit)
    return {"bets": bets}


# ------------------------------------------------------------
# Ставки: добавить / рассчитать / статистика / банк
# ------------------------------------------------------------

@app.post("/agent/add-bet")
def api_add_bet(
    bet: BetCreate,
    session: Session = Depends(get_session),
):
    bet_id = add_bet(
        session,
        user_id=bet.user_id,
        market=bet.market,
        odds=bet.odds,
        stake=bet.stake,
    )
    return {"bet_id": bet_id}


@app.post("/agent/settle-bet")
def api_settle_bet(
    data: BetSettle,
    session: Session = Depends(get_session),
):
    settle_bet(session, data.user_id, data.bet_id, data.result)
    return {"status": "ok"}


@app.get("/agent/stats")
def api_user_stats(
    user_id: int,
    session: Session = Depends(get_session),
):
    stats = get_user_stats(session, user_id)
    return stats


@app.get("/agent/bank")
def api_user_bank(
    user_id: int,
    session: Session = Depends(get_session),
):
    return get_user_bank(session, user_id)


@app.post("/agent/bank/set")
def api_set_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    set_user_bank(session, user_id, amount)
    return {"status": "ok"}


@app.post("/agent/bank/change")
def api_change_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    change_user_bank(session, user_id, amount)
    return {"status": "ok"}


@app.get("/agent/all-bets")
def api_all_bets(
    user_id: int,
    session: Session = Depends(get_session),
):
    bets = get_all_bets(session, user_id)
    return {"bets": bets}


# ------------------------------------------------------------
# KHL-эндпоинты
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
