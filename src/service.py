# src/service.py
from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from .db import init_db
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
    message: str   # важно: ИМЕННО message – под это шлёт телеграм-бот


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
    result: str  # "win", "lose", "push"


# ------------------------------------------------------------
# Инициализация БД
# ------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("FastAPI сервис запущен (бот работает в отдельном процессе).")


# ------------------------------------------------------------
# Эндпоинты агента
# ------------------------------------------------------------

@app.post("/agent/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """
    Главная точка входа для Telegram-бота.

    Здесь максимально аккуратно оборачиваем импорт и вызов run_dialog_agent,
    чтобы не было 500 и чтобы видеть понятный текст ошибки.
    """
    logger.info("/agent/query: user_id=%s, message=%r", req.user_id, req.message)

    try:
        from .parsing import run_dialog_agent
    except Exception as e:
        logger.exception("Не удалось импортировать run_dialog_agent")
        return QueryResponse(
            reply=f"⚠️ Ошибка сервера (импорт агента): {type(e).__name__}: {e}"
        )

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

    return QueryResponse(reply=reply)


@app.get("/agent/last-bets")
def api_last_bets(user_id: int, limit: int = 5):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        bets = get_last_bets(session, user_id, limit)
        # Преобразуем в dict’ы для JSON
        return {"bets": [b.dict() for b in bets]}
    finally:
        gen.close()


@app.post("/agent/add-bet")
def api_add_bet(bet: BetCreate):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        db_bet = add_bet(
            session=session,
            user_id=bet.user_id,
            raw_text="",          # в MVP сырое описание не передаём
            stake=bet.stake,
            odds=bet.odds,
            event=bet.market,
            outcome=None,
        )
        return {"bet_id": db_bet.id}
    finally:
        gen.close()


@app.post("/agent/settle-bet")
def api_settle_bet(data: BetSettle):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        settle_bet(session, data.user_id, data.bet_id, data.result)
        return {"status": "ok"}
    finally:
        gen.close()


@app.get("/agent/stats")
def api_user_stats(user_id: int):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        stats = get_user_stats(session, user_id)
        return stats.__dict__
    finally:
        gen.close()


@app.get("/agent/bank")
def api_user_bank(user_id: int):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        bank = get_user_bank(session, user_id)
        return {"bank": bank}
    finally:
        gen.close()


@app.post("/agent/bank/set")
def api_set_user_bank(user_id: int, amount: float):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        set_user_bank(session, user_id, amount)
        return {"status": "OK"}
    finally:
        gen.close()


@app.post("/agent/bank/change")
def api_change_user_bank(user_id: int, amount: float):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        change_user_bank(session, user_id, amount)
        return {"status": "OK"}
    finally:
        gen.close()


@app.get("/agent/all-bets")
def api_all_bets(user_id: int):
    from .db import get_session
    from sqlmodel import Session

    gen = get_session()
    session: Session = next(gen)
    try:
        bets = get_all_bets(session, user_id)
        return {"bets": [b.dict() for b in bets]}
    finally:
        gen.close()


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
