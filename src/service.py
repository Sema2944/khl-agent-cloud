# src/service.py
from fastapi import FastAPI

# ВАЖНО: переменная должна называться ровно "app"
app = FastAPI(title="KHL Agent API")

@app.get("/healthz")
def healthz():
    return {"ok": True}
