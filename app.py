"""Chat server: FastAPI backend for the custom web UI in web/.

    uv run python app.py        ->  http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rag import settings
from rag.workflow import ask, ask_stream

WEB_DIR = Path(__file__).parent / "web"
MAX_HISTORY_MESSAGES = 8


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(settings.warm_up)  # first customer question should not pay for client setup
    yield


app = FastAPI(title="FNX site bot", lifespan=lifespan)


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = Field(default_factory=list)
    page: str = Field(default="", max_length=200)  # title of the fnx.co.il page (browser extension)


def _history(req: ChatRequest) -> list[dict]:
    return [m.model_dump() for m in req.history[-MAX_HISTORY_MESSAGES:]]


def _public(out: dict) -> dict:
    return {"text": out["text"], "route": out["route"], "sources": out["sources"]}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    return _public(await ask(req.message, _history(req), req.page))


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Server-sent events: {"type":"token","delta":...}* then {"type":"final",...}."""

    async def events():
        try:
            async for kind, payload in ask_stream(req.message, _history(req), req.page):
                data = {"type": "token", "delta": payload} if kind == "token" else {"type": "final", **_public(payload)}
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as e:  # surface failures to the UI instead of a silently dropped stream
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    """Cheap wake-up target (keep-awake ping, page load): no model or vector calls."""
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "favicon.ico")  # browsers also ask the root path directly


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
