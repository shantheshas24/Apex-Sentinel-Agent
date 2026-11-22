"""
ChromaDB vector memory for user deadline preferences.

All chroma calls are wrapped in asyncio.to_thread() since chromadb
PersistentClient is synchronous and would block the event loop.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

DEADLINE_PREFERENCE_CATEGORIES = ("deadline", "general", "email")
DEADLINE_PREFERENCE_FILTER = {"category": {"$in": list(DEADLINE_PREFERENCE_CATEGORIES)}}

# Apply HuggingFace token so model downloads are authenticated (higher rate limits)
_hf_token = os.getenv("HUGGINGFACE_TOKEN") or os.getenv("HF_TOKEN")
if _hf_token:
    os.environ["HF_TOKEN"] = _hf_token
    os.environ["HUGGINGFACE_TOKEN"] = _hf_token


class ChromaMemory:
    _instance: "ChromaMemory | None" = None

    def __init__(self, persist_path: str, embedding_model: str) -> None:
        self._client = chromadb.PersistentClient(path=persist_path)
        self._ef = SentenceTransformerEmbeddingFunction(
            model_name=embedding_model,
            device="cpu",
            normalize_embeddings=True,
        )
        self.preferences = self._client.get_or_create_collection(
            name="user_preferences",
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls) -> "ChromaMemory":
        from config.settings import get_settings
        cfg = get_settings()
        return cls(cfg.chroma_persist_path, cfg.chroma_embedding_model)

    @classmethod
    def get_instance(cls) -> "ChromaMemory":
        """Return the shared singleton — creates it on first call."""
        if cls._instance is None:
            cls._instance = cls.from_settings()
        return cls._instance

    # ── User Preferences ──────────────────────────────────────────────────────

    async def store_preference(
        self,
        text: str,
        category: str = "general",
        source: str = "inferred",
        priority: int = 2,
    ) -> None:
        """
        Store a natural-language preference statement as an embedding.
        Example: "User prioritizes assignments from professor@uni.edu"
        """
        doc_id = f"pref-{uuid.uuid4()}"
        meta = {
            "category": category,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "priority": priority,
        }
        await asyncio.to_thread(
            self.preferences.add,
            documents=[text],
            metadatas=[meta],
            ids=[doc_id],
        )

    async def query_preferences(
        self, query: str, n_results: int = 3
    ) -> list[dict[str, Any]]:
        """
        Semantic search over stored preferences.
        Returns list of {document, metadata, distance}.
        """
        results = await asyncio.to_thread(
            self.preferences.query,
            query_texts=[query],
            n_results=n_results,
            where=DEADLINE_PREFERENCE_FILTER,
        )
        output = []
        if results and results["documents"]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                output.append({"document": doc, "metadata": meta, "distance": dist})
        return output

    async def seed_default_preferences(self) -> None:
        """
        Seed sensible default preferences on first run so the planner
        has context even before the user has a history.
        """
        count = await asyncio.to_thread(self.preferences.count)
        if count > 0:
            return  # already seeded

        defaults = [
            ("User prioritizes deadlines from professors, managers, and important contacts", 1),
            ("User wants to be notified immediately about urgent deadlines", 1),
        ]
        for text, priority in defaults:
            await self.store_preference(text, category="deadline", priority=priority, source="default")
