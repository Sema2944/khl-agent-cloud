import uvicorn, os

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    # теперь указываем на корневой файл service.py
    uvicorn.run("service:app", host="0.0.0.0", port=port, reload=False)

    return {"picks": picks}
