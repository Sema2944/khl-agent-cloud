# src/service.py

from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlmodel import Session

from .db import init_db, get_session
from .bets_db import get_user_stats
from .khl_client import get_today_khl_events

app = FastAPI(title="KHL AI Betting Agent")


class AgentQuery(BaseModel):
    user_id: int
    message: str


class AgentResponse(BaseModel):
    reply: str


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"status": "ok", "service": "khl-agent"}


@app.post("/agent/query", response_model=AgentResponse)
async def agent_query(
    payload: AgentQuery,
    session: Session = Depends(get_session),
) -> AgentResponse:
    reply_text = await run_agent(
        user_id=payload.user_id,
        message=payload.message,
        session=session,
    )
    return AgentResponse(reply=reply_text)


# ------------------ ЛОГИКА АГЕНТА ------------------


async def run_agent(user_id: int, message: str, session: Session) -> str:
    text = (message or "").lower().strip()

    # 1) Показать статистику по ставкам
    if "статист" in text or "статку" in text or "stats" in text:
        stats = get_user_stats(session, user_id)
        if stats.total_bets == 0:
            return "Пока нет ни одной сохранённой ставки. Начнём с первой 😉"

        return (
            "Твоя статистика:\n"
            f"Ставок: {stats.total_bets}\n"
            f"Винрейт: {stats.winrate:.1f}%\n"
            f"ROI: {stats.roi:.2f}%\n"
            f"Плюс/минус: {stats.pnl:.0f} ₽"
        )

    # 2) Матчи КХЛ на сегодня
    if "кхл" in text and ("сегодня" in text or "на сегодня" in text):
        events = await get_today_khl_events()
        if not events:
            return "На сегодня я не нашёл матчей КХЛ."

        lines = []
        for e in events[:5]:
            line = f"{e.team1} — {e.team2} (id: {e.id})"
            market_1x2 = next((m for m in e.markets if m.name == "1X2"), None)
            if market_1x2:
                odds_part = ", ".join(
                    f"{o.name}: {o.price}" for o in market_1x2.outcomes
                )
                line += f" | 1X2: {odds_part}"
            lines.append(line)

        return "Матчи КХЛ на сегодня:\n" + "\n".join(lines)

    # 3) Заглушка под добавление ставки
    if text.startswith("ставка ") or text.startswith("поставь "):
        return (
            "Я уже понимаю, что ты хочешь сделать ставку,\n"
            "но пока не сохраняю её в базу.\n"
            "На следующем шаге добавим парсинг и запись в БД 💾"
        )

    # 4) Ответ по умолчанию
    return (
        "Я AI-агент для ставок.\n"
        "Сейчас умею:\n"
        "• По словам 'статистика / статку' показывать твою статистику\n"
        "• По запросу 'КХЛ сегодня' показывать матчи КХЛ\n\n"
        "Попробуй: 'Покажи мою статистику' или 'Какие матчи КХЛ сегодня?'."
    )
