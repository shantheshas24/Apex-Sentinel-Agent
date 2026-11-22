"""
Health check endpoint — live status of all backend services.
  GET /api/health  → Redis, LLM, Google, Twilio, ChromaDB, MCP server
"""
import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from personal_os_agent.api.routers.google import connection_status

router = APIRouter()


@router.get("/api/health")
async def health_check(request: Request) -> dict:
    """Live health status of all services."""
    from personal_os_agent.config.settings import get_settings
    cfg = get_settings()
    checks: dict[str, dict] = {}
    overall = True

    # Redis
    try:
        from personal_os_agent.memory.redis_client import RedisClient
        redis = RedisClient.get_instance()
        t0 = time.perf_counter()
        ok = await redis.ping()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        overall = ok
        checks["redis"] = {
            "status": "ok" if ok else "error",
            "latency_ms": ms,
            "queues": await redis.scan_queue_depths(),
        }
    except Exception as e:
        checks["redis"] = {"status": "error", "error": type(e).__name__}
        overall = False

    if getattr(request.app.state, "dashboard_only", False):
        return {
            "overall": "healthy" if overall else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": checks,
        }

    # OpenRouter / Groq LLM
    try:
        if not cfg.openrouter_api_key:
            checks["openrouter"] = {"status": "error", "error": "OPENROUTER_API_KEY not set"}
            overall = False
        else:
            import httpx
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=5.0) as hclient:
                r = await hclient.get(
                    f"{cfg.openrouter_base_url}/models",
                    headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
                )
            ms = round((time.perf_counter() - t0) * 1000, 1)
            checks["openrouter"] = (
                {"status": "ok", "model": cfg.openrouter_model, "latency_ms": ms}
                if r.status_code == 200
                else {"status": "error", "error": f"HTTP {r.status_code}"}
            )
    except Exception as e:
        checks["openrouter"] = {"status": "error", "error": type(e).__name__}
        overall = False

    # Google APIs
    checks["google"] = await connection_status(request)
    overall = overall and checks["google"]["status"] == "ok"

    # Twilio
    if cfg.deadline_phone_available:
        try:
            from twilio.rest import Client
            t0   = time.perf_counter()
            tw   = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
            await asyncio.to_thread(lambda: tw.api.accounts(cfg.twilio_account_sid).fetch())
            ms   = round((time.perf_counter() - t0) * 1000, 1)
            checks["twilio"] = {
                "status": "ok",
                "latency_ms": ms,
            }
        except Exception as e:
            checks["twilio"] = {"status": "error", "error": type(e).__name__}
            overall = False
    else:
        checks["twilio"] = {"status": "disabled"}

    # ChromaDB
    try:
        import chromadb
        t0   = time.perf_counter()
        client = await asyncio.to_thread(chromadb.PersistentClient, cfg.chroma_persist_path)
        cols   = await asyncio.to_thread(client.list_collections)
        ms     = round((time.perf_counter() - t0) * 1000, 1)
        checks["chromadb"] = {
            "status": "ok",
            "collections": [c.name for c in cols],
            "latency_ms": ms,
        }
    except Exception as e:
        checks["chromadb"] = {"status": "error", "error": type(e).__name__}
        overall = False

    # MCP Server — FastMCP returns 404 on root; any HTTP response confirms the app is up.
    mcp_root = f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port}/"
    try:
        import httpx
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=5.0) as hclient:
            await hclient.get(mcp_root)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        checks["mcp_server"] = {"status": "ok", "url": cfg.mcp_sse_url, "latency_ms": ms}
    except Exception:
        checks["mcp_server"] = {"status": "error", "url": cfg.mcp_sse_url, "error": "Not reachable"}

    return {
        "overall": "healthy" if overall else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    }
