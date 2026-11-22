"""
Preferences endpoints — ChromaDB user preference management.
  GET  /api/preferences  → list all stored preference statements
  POST /api/preferences  → add a new natural-language preference
"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/preferences")
async def list_preferences() -> list[dict]:
    """List all ChromaDB user preference documents."""
    from memory.chroma_memory import ChromaMemory, DEADLINE_PREFERENCE_FILTER
    memory = ChromaMemory.from_settings()
    try:
        result = await asyncio.to_thread(
            memory.preferences.get,
            where=DEADLINE_PREFERENCE_FILTER,
            include=["documents", "metadatas"],
        )
        return [
            {"document": doc, "metadata": meta}
            for doc, meta in zip(
                result.get("documents", []),
                result.get("metadatas", []),
            )
        ]
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/preferences")
async def add_preference(body: dict) -> dict:
    """Add a new natural-language preference to ChromaDB."""
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    category = body.get("category", "deadline")
    from memory.chroma_memory import ChromaMemory, DEADLINE_PREFERENCE_CATEGORIES
    if category not in DEADLINE_PREFERENCE_CATEGORIES:
        return JSONResponse({"error": "Unsupported preference category"}, status_code=400)
    memory = ChromaMemory.from_settings()
    try:
        await memory.store_preference(text, category=category, source="dashboard")
        return {"status": "saved", "text": text, "category": category}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
