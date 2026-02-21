# winline_proxy.py
from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI()

WINLINE_BASE_URL = "https://cf.winlinesports.com/v3"


@app.get("/events/list")
async def proxy_events_list(lang: str = "ru"):
    """
    Прокси на: GET /v3/events/list?lang=ru
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{WINLINE_BASE_URL}/events/list",
                params={"lang": lang},
            )
        r.raise_for_status()
        data = r.json()
        # Просто возвращаем JSON как есть
        return JSONResponse(content=data)
    except httpx.HTTPError as e:
        logger.exception("Ошибка прокси /events/list: %s", e)
        raise HTTPException(status_code=502, detail="Ошибка при запросе к Winline")


@app.get("/events/{event_id}/markets")
async def proxy_event_markets(event_id: int, lang: str = "ru"):
    """
    Прокси на: GET /v3/events/{id}/markets?lang=ru
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{WINLINE_BASE_URL}/events/{event_id}/markets",
                params={"lang": lang},
            )
        r.raise_for_status()
        data = r.json()
        return JSONResponse(content=data)
    except httpx.HTTPError as e:
        logger.exception("Ошибка прокси /events/%s/markets: %s", event_id, e)
        raise HTTPException(status_code=502, detail="Ошибка при запросе к Winline")
