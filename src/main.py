from fastapi import FastAPI

from src.api.routes import router

app = FastAPI(title="Options Protocol", version="0.1.0")
app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}
