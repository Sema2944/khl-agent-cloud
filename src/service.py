from __future__ import annotations
from fastapi import FastAPI
from pydantic import BaseModel
import numpy as np

app = FastAPI(title="KHL Agent API")

class BetsIn(BaseModel):
    rows: list[dict]
    edge_min: float | None = 0.02
    kelly_k: float | None = 0.25
    max_picks: int | None = 5

@app.get("/healthz")
def healthz(): return {"ok": True}

@app.post("/khl/bets_1x2")
def khl_bets(inp: BetsIn):
    # демо-ответ (имитируем сигнал)
    picks=[]
    for r in inp.rows[: inp.max_picks or 5]:
        picks.append({
            "game_id": r["game_id"], "home": r["team_id"], "away": r["opp_id"],
            "selection": "1", "odds": float(r["odds_1"]),
            "p_model": 0.53, "edge": 0.04, "stake": 1.25
        })
    return {"picks": picks}
