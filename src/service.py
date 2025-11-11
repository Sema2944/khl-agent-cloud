# src/service.py
from fastapi import FastAPI

# ВАЖНО: имя ПЕРЕМЕННОЙ — именно "app"
app = FastAPI(title="KHL Agent API")

@app.get("/healthz")
def healthz():
    return {"ok": True}
