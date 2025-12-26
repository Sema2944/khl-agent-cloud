# src/service.py
import logging
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

# ✅ ВАЖНО: монтируем Telegram webhook РАНЬШЕ startup,
# чтобы маршрут /telegram/webhook был зарегистрирован до начала работы.
try:
    from .telegram_bot.app import mount as mount_telegram

    mount_telegram(app)
    logger.info("Telegram routes mounted.")
except Exception as e:
    # не падаем, чтобы API продолжал жить даже если телега не настроена
    logger.exception("Failed to mount telegram webhook routes: %s", e)


@app.get("/__health")
def health():
    return {"ok": True}


# -----------------------------
# Healthcheck (Render)
# -----------------------------
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "service": "khl-agent-api"}


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
# Startup
# -----------------------------
@app.on_event("startup")
def on_startup():
    init_db()
    logger.info(
        "FastAPI сервис запущен (DB ok). Telegram webhook init is handled in telegram_bot/app.py startup hook."
    )


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
        return QueryResponse(reply=f"⚠️ Ошибка сервера (импорт агента): {type(e).__name__}: {e}")

    try:
        reply = await run_dialog_agent(user_id=req.user_id, message=text)
    except Exception as e:
        logger.exception("Ошибка внутри run_dialog_agent")
        return QueryResponse(reply=f"⚠️ Внутренняя ошибка агента: {type(e).__name__}: {e}")

    return QueryResponse(reply=reply)


# -----------------------------
# Bets API
# -----------------------------
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
