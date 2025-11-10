from fastapi import FastAPI

app = FastAPI(title="KHL Agent API (test)")

@app.get("/healthz")
def healthz():
    return {"ok": True}
