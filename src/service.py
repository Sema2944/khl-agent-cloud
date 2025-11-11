import uvicorn, os
if __name__ == "__main__":
    port = int(os.getenv("PORT","8000"))
    uvicorn.run("src.service:app", host="0.0.0.0", port=port, reload=False)
