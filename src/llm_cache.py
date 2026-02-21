# src/llm_cache.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
LLM_CACHE_TTL_S = int((os.getenv("LLM_CACHE_TTL_S") or "21600").strip())  # 6h default
LLM_CACHE_PREFIX = (os.getenv("LLM_CACHE_PREFIX") or "khl:llm").strip()

# Важно: чтобы не убить SLA — очень короткие таймауты на кэш
REDIS_IO_TIMEOUT_S = float((os.getenv("REDIS_IO_TIMEOUT_S") or "0.15").strip())  # 150ms
REDIS_CONNECT_TIMEOUT_S = float((os.getenv("REDIS_CONNECT_TIMEOUT_S") or "0.2").strip())  # 200ms


@dataclass
class CacheGetResult:
    hit: bool
    value: Optional[dict] = None
    source: str = "none"  # redis | memory | none


class _InMemoryCache:
    """Fallback если Redis не настроен или временно недоступен."""
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}  # key -> (expires_at, json_str)

    def get(self, key: str) -> Optional[dict]:
        item = self._data.get(key)
        if not item:
            return None
        expires_at, payload = item
        if expires_at <= time.time():
            self._data.pop(key, None)
            return None
        try:
            return json.loads(payload)
        except Exception:
            self._data.pop(key, None)
            return None

    def set(self, key: str, value: dict, ttl_s: int) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except Exception:
            return
        self._data[key] = (time.time() + max(1, ttl_s), payload)


_mem = _InMemoryCache()

_redis_client = None
_redis_enabled = False


def _redis_import():
    # ленивый импорт, чтобы не падать если redis-пакет не поставили
    import redis.asyncio as redis  # type: ignore
    return redis


async def init_cache() -> None:
    """
    Вызывать 1 раз на старте (опционально).
    Если не вызовешь — тоже ок, мы лениво подключимся при первом запросе.
    """
    global _redis_client, _redis_enabled
    if not REDIS_URL:
        _redis_enabled = False
        logger.info("LLM cache: REDIS_URL not set -> using in-memory only.")
        return

    try:
        redis = _redis_import()
        _redis_client = redis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_S,
            socket_timeout=REDIS_IO_TIMEOUT_S,
        )
        # ping быстро, не блокируем SLA
        await asyncio.wait_for(_redis_client.ping(), timeout=REDIS_IO_TIMEOUT_S)
        _redis_enabled = True
        logger.info("LLM cache: Redis enabled.")
    except Exception as e:
        _redis_client = None
        _redis_enabled = False
        logger.warning("LLM cache: Redis unavailable -> fallback to memory. err=%s", e)


def make_cache_key(*, prompt_hash: str, line_hash: str) -> str:
    # ключ стабильный, совместим с масштабированием
    return f"{LLM_CACHE_PREFIX}:{prompt_hash}:{line_hash}"


async def cache_get(key: str) -> CacheGetResult:
    # 1) Redis (если включен)
    if REDIS_URL:
        # лениво инициализируем (если init_cache не вызвали)
        global _redis_enabled
        if _redis_client is None and not _redis_enabled:
            await init_cache()

        if _redis_enabled and _redis_client is not None:
            try:
                raw = await asyncio.wait_for(_redis_client.get(key), timeout=REDIS_IO_TIMEOUT_S)
                if raw:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        return CacheGetResult(hit=True, value=obj, source="redis")
            except Exception as e:
                # Не падаем, просто деградируем
                logger.debug("LLM cache: Redis get failed -> memory fallback. err=%s", e)

    # 2) In-memory fallback
    obj = _mem.get(key)
    if obj is not None:
        return CacheGetResult(hit=True, value=obj, source="memory")

    return CacheGetResult(hit=False, value=None, source="none")


async def cache_set(key: str, value: dict, ttl_s: int = LLM_CACHE_TTL_S) -> None:
    ttl_s = int(max(5, ttl_s))

    # 1) Redis
    if REDIS_URL:
        global _redis_enabled
        if _redis_client is None and not _redis_enabled:
            await init_cache()

        if _redis_enabled and _redis_client is not None:
            try:
                raw = json.dumps(value, ensure_ascii=False)
                # setex: ttl + value
                await asyncio.wait_for(_redis_client.setex(key, ttl_s, raw), timeout=REDIS_IO_TIMEOUT_S)
                return
            except Exception as e:
                logger.debug("LLM cache: Redis set failed -> memory fallback. err=%s", e)

    # 2) In-memory fallback
    _mem.set(key, value, ttl_s)
