# src/service.py
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session
from .bets_db import (
    get_user_stats as db_get_user_stats,
    add_bet as db_add_bet,
    get_last_bets as db_get_last_bets,
    settle_bet as db_settle_bet,
    get_user_bank as db_get_user_bank,
    set_user_bank as db_set_user_bank,
    change_user_bank as db_change_user_bank,
    get_all_bets as db_get_all_bets,
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
    user_id: int
    message: str   # важно: ИМЕННО message — под это шлёт Telegram-бот


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
    result: str  # "win", "lose", "push" (или русские аналоги, см. bets_db.settle_bet)


# ------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ БД
# ------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("FastAPI сервис запущен (бот работает в отдельном процессе).")


# ------------------------------------------------------------
# ЭНДПОИНТ АГЕНТА (LLM)
# ------------------------------------------------------------

@app.post("/agent/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """
    Главная точка входа для Telegram-бота.

    Здесь аккуратно оборачиваем импорт и вызов run_dialog_agent,
    чтобы не было 500 и чтобы видеть понятный текст ошибки.
    """
    logger.info("/agent/query: user_id=%s, message=%r", req.user_id, req.message)

    # 1) Пытаемся импортировать агент
    try:
        from .parsing import run_dialog_agent
    except Exception as e:
        logger.exception("Не удалось импортировать run_dialog_agent")
        return QueryResponse(
            reply=f"⚠️ Ошибка сервера (импорт агента): {type(e).__name__}: {e}"
        )

    # 2) Пытаемся вызвать агента
    try:
        reply = await run_dialog_agent(
            user_id=req.user_id,
            message=req.message,
        )
    except Exception as e:
        logger.exception("Ошибка внутри run_dialog_agent")
        return QueryResponse(
            reply=f"⚠️ Внутренняя ошибка агента: {type(e).__name__}: {e}"
        )

    # 3) Всё ок — отдаём нормальный ответ
    return QueryResponse(reply=reply)


# ------------------------------------------------------------
# ЭНДПОИНТЫ РАБОТЫ СО СТАВКАМИ / БАНКОМ
# ------------------------------------------------------------

@app.get("/agent/last-bets")
def api_last_bets(
    user_id: int,
    limit: int = 5,
    session: Session = Depends(get_session),
):
    bets = db_get_last_bets(session, user_id, limit)
    # Преобразуем Bet в dict, FastAPI сам умеет, но явно приведём
    return {"bets": [b.dict() for b in bets]}


@app.post("/agent/add-bet")
def api_add_bet(
    bet: BetCreate,
    session: Session = Depends(get_session),
):
    new_bet = db_add_bet(
        session=session,
        user_id=bet.user_id,
        raw_text=bet.raw_text,
        stake=bet.stake,
        odds=bet.odds,
        event=bet.event,
        outcome=bet.outcome,
    )
    return {"bet_id": new_bet.id}


@app.post("/agent/settle-bet")
def api_settle_bet(
    data: BetSettle,
    session: Session = Depends(get_session),
):
    bet = db_settle_bet(session, data.user_id, data.bet_id, data.result)
    if bet is None:
        return {"status": "error", "error": "Bet not found or invalid result"}
    return {"status": "ok", "profit": bet.profit}


@app.get("/agent/stats")
def api_user_stats(
    user_id: int,
    session: Session = Depends(get_session),
):
    stats = db_get_user_stats(session, user_id)
    # dataclass → dict
    return {
        "total_bets": stats.total_bets,
        "settled_bets": stats.settled_bets,
        "pushes": stats.pushes,
        "winrate": stats.winrate,
        "roi": stats.roi,
        "pnl": stats.pnl,
        "total_stake": stats.total_stake,
    }


@app.get("/agent/bank")
def api_user_bank(
    user_id: int,
    session: Session = Depends(get_session),
):
    bank = db_get_user_bank(session, user_id)
    return {"bank": bank}


@app.post("/agent/bank/set")
def api_set_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    user = db_set_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": user.bank}


@app.post("/agent/bank/change")
def api_change_user_bank(
    user_id: int,
    amount: float,
    session: Session = Depends(get_session),
):
    user = db_change_user_bank(session, user_id, amount)
    return {"status": "ok", "bank": user.bank}


@app.get("/agent/all-bets")
def api_all_bets(
    user_id: int,
    session: Session = Depends(get_session),
):
    bets = db_get_all_bets(session, user_id)
    return {"bets": [b.dict() for b in bets]}


# ------------------------------------------------------------
# KHL-ЭНДПОИНТЫ (можно использовать как API для фронта/бота)
# ------------------------------------------------------------

@app.get("/khl/today")
async def api_khl_today():
    """
    Текстовый обзор матчей КХЛ на сегодня.
    """
    text = await khl_today_text_from_winline()
    return {"text": text}


@app.get("/khl/match-context/{event_id}")
async def api_match_context(event_id: int):
    """
    Собираем контекст по конкретному матчу:
    – команды, форма, рынки и т.п.
    """
    events = await get_khl_events_today()
    event = next((e for e in events if e["id"] == event_id), None)

    if not event:
        return {"error": "Match not found"}

    ctx = await build_match_context_notes(event)
    return ctx


@app.get("/khl/team-form/{team}")
async def api_team_form(team: str):
    """
    Форма команды (базовая/расширенная).
    """
    form: TeamForm | TeamAdvancedForm = await get_team_form(team)
    return form.dict()
