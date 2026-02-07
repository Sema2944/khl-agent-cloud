# src/jobs/routes.py
from __future__ import annotations

import os
import logging
from fastapi import APIRouter, HTTPException, Request

from src.telegram_bot.app import _telegram_app
from src.jobs.daily_pro import run_daily_pro

logger = logging.getLogger(__name__)
router = APIRouter()

JOB_KEY = (os.getenv("DAILY_PRO_JOB_KEY") or "").strip()

@router.post("/jobs/daily-pro")
async def daily_pro_job(request: Request):
    key = request.query_params.get("key", "")
    if not JOB_KEY:
        raise HTTPException(status_code=503, detail="DAILY_PRO_JOB_KEY missing")
    if key != JOB_KEY:
        raise HTTPException(status_code=403, detail="forbidden")

    if _telegram_app is None:
        raise HTTPException(status_code=503, detail="telegram app not initialized")

    try:
        await run_daily_pro(_telegram_app.bot)
        return {"ok": True}
    except Exception:
        logger.exception("daily-pro job failed")
        raise HTTPException(status_code=500, detail="job failed")
