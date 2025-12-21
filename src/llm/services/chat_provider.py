# llm/services/chat_provider.py
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sqlite3
import threading
import time
from typing import Any

from openai import AsyncOpenAI, OpenAIError
from shared_pkg.config.settings import Settings
from shared_pkg.utils.logging_config import get_logger
from llm.models.chat import ChatCompletionRequest
from llm.services.cache import SQLiteCache

logger = get_logger(__name__)


def _resolve_cache_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "llm_cache_enabled", False))


def _resolve_cache_ttl_seconds(settings: Settings) -> int:
    return int(getattr(settings, "llm_cache_ttl", 0) or 0)


def _resolve_cache_path(settings: Settings) -> str:
    return settings.llm_cache_path_resolved

class OpenAIChatProvider:
    def __init__(self, settings: Settings, *, cache: SQLiteCache | None = None):
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=str(settings.llm_api_base),
            timeout=settings.llm_timeout,
        )
        self._base_url = str(settings.llm_api_base)
        self._default_model = settings.llm_model_name
        self._default_temperature = settings.llm_temperature
        self._default_top_p = settings.llm_top_p
        self._cache = cache or SQLiteCache(
            path=settings.llm_cache_path,
            ttl_seconds=settings.llm_cache_ttl,
            enabled=settings.llm_cache_enabled,
        )

        # cache config from "old vars"
        self._cache_enabled = _resolve_cache_enabled(settings)
        self._cache_ttl = _resolve_cache_ttl_seconds(settings)
        self._cache_path = _resolve_cache_path(settings)

        if self._cache_enabled:
            cache_dir = os.path.dirname(self._cache_path)
            if self._cache_path != ":memory:" and cache_dir:
                os.makedirs(cache_dir, exist_ok=True)

            self._cache_lock = threading.Lock()
            self._conn = sqlite3.connect(self._cache_path, check_same_thread=False, timeout=30)
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS cache("
                "  key TEXT PRIMARY KEY, value BLOB, ts INTEGER"
                ")"
            )
            if self._cache_ttl > 0:
                cutoff = int(time.time()) - self._cache_ttl
                cur.execute("DELETE FROM cache WHERE ts < ?", (cutoff,))
            self._conn.commit()

            logger.info("Chat cache enabled: path=%s ttl=%ss", self._cache_path, self._cache_ttl)

    def _cache_key(self, payload: dict[str, Any]) -> str:
        # Namespace to avoid collisions with other cached objects
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return "chat:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Any | None:
        with self._cache_lock:
            row = self._conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        val, ts = row
        if self._cache_ttl > 0 and time.time() - ts > self._cache_ttl:
            with self._cache_lock:
                self._conn.execute("DELETE FROM cache WHERE key=?", (key,))
                self._conn.commit()
            return None
        return pickle.loads(val)

    def _cache_set(self, key: str, value: Any) -> None:
        blob = pickle.dumps(value)
        with self._cache_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache(key, value, ts) VALUES(?, ?, ?)",
                (key, blob, int(time.time())),
            )
            self._conn.commit()

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        payload = request.to_provider_payload(
            default_model=self._default_model,
            default_temperature=self._default_temperature,
            default_top_p=self._default_top_p,
        )

        if self._cache_enabled:
            key = self._cache_key(payload)
            cached = self._cache_get(key)
            if cached is not None:
                logger.info("[CACHE HIT] /v1/chat key=%s model=%s", key, payload.get("model"))
                return cached
            logger.info("[CACHE MISS] /v1/chat key=%s model=%s", key, payload.get("model"))

        try:
            response = await self._client.chat.completions.create(**payload)
        except OpenAIError:
            logger.exception("Provider error while executing chat completion")
            raise

        body = response.model_dump() if hasattr(response, "model_dump") else response.dict()

        if self._cache_enabled:
            # игнорируем ошибку pyright, потому что точно знаем - если _cache_enabled, то key мы выше посчитали
            self._cache_set(key, body) # pyright: ignore

        return body
