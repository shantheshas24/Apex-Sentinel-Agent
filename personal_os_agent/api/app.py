"""
PersonalOS FastAPI Application — port 8080.

Endpoints are split into focused routers:
  api/routers/inbox.py       — Gmail scan and activity feed
  api/routers/events.py      — synthetic event injection
  api/routers/health.py      — live service health checks
  api/routers/metrics.py     — aggregate stats / confidence histogram
  api/routers/preferences.py — ChromaDB user preference CRUD

Static assets served from api/static/:
  dashboard.html             — single-page frontend
  style.css                  — all CSS
  app.js                     — all JavaScript

WebSocket:
  api/ws.py                  — ConnectionManager + /ws endpoint
"""
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from personal_os_agent.api.ws import manager
from personal_os_agent.api.routers import deadlines, events, google, health, inbox, metrics, preferences

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="PersonalOS Deadline Dashboard", version="1.3.0")

# ── Static files ───────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(deadlines.router)
app.include_router(events.router)
app.include_router(google.router)
app.include_router(health.router)
app.include_router(inbox.router)
app.include_router(metrics.router)
app.include_router(preferences.router)


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Pushes 'refresh' events to all connected dashboards."""
    await manager.connect(websocket)
    try:
        while True:
            await __import__("asyncio").sleep(20)
            await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ── Dashboard UI ───────────────────────────────────────────────────────────────
@app.get("/")
async def dashboard_page(request: Request) -> Response:
    if "code" in request.query_params or "error" in request.query_params:
        return await google.finish_connection(request)
    return FileResponse(_STATIC_DIR / "dashboard.html")
