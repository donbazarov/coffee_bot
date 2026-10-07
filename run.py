import os

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "bot.web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8000")),
    )