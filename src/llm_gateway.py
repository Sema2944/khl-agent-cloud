# src/llm_gateway.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy

# cooldown defaults (seconds)
LLM_429_COOLDOWN_DEFAULT_S = int((os.getenv("LLM_429_COOLDOWN_DEFAULT_S") or "600").strip())
LLM_429_COOLDOWN_MAX_S = int((os.getenv("LLM_429_COOLDOWN_MAX_S") or "3600").strip())

# soft limits / housekeeping
GW_MAX_CACHE_ITEMS = int((os.getenv("GW_MAX_CACHE_ITEMS") or "2000").strip())
GW_CLEANUP_EVERY_S = int((os.getenv("GW_CLEANUP_EVERY_S") or "30").strip())

# -----------------------------
# Types expected by llm_client
# -----------------------------
@dataclass
class GatewayMeta:
    provider: str
    elapsed_ms: int
    used_fallback: bool
    last_error: Optional[str]
    cache: str  # "hit" | "miss" | "dedupe"


# call_llm_fn contract from llm_client:
# returns:
#   (obj_or_none, meta_like_dict, headers_dict)
CallLLMFn = Callable[[], Awaitable[Tuple[Optional[Any], Dict[str, Any], Dict[str, str]]]]


# -----------------------------
# Helpers
# -----------------------------
def _wall_now() -> float:
    return time.time()


def _mono_now() -> float:
    return time.monotonic()


def _parse_retry_after(headers: Dict[str, str]) -> Optional[int]:
    """
    retry-after may be seconds string. We keep it simple.
    """
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if not ra:
        return None
    try:
        sec = int(float(str(ra).strip()))
        return sec if sec > 0 else None
    except Exception:
        return None


def _cap_cooldown(sec: int) -> int:
    sec = max(1, int(sec))
    return min(sec, LLM_429_COOLDOWN_MAX_S)


# -----------------------------
# In-memory gateway (MVP)
# -----------------------------
class LLMGateway:
    """
    MVP gateway:
    - cache (process-local)
    - in-flight dedupe (same cache_key)
    - global cooldown after 429
    """

    def __init__(self, provider: str):
        self.provider = provider

        # cache_key -> (exp_ts, obj)
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_lock = asyncio.Lock()

        # cache_key -> asyncio.Task producing (obj, GatewayMeta)
        self._inflight: Dict[str, asyncio.Task] = {}
        self._inflight_lock = asyncio.Lock()

        # global cooldown
        self._cooldown_until_ts: float = 0.0
        self._cooldown_reason: str = ""
        self._cooldown_lock = asyncio.Lock()

        # cleanup
        self._last_cleanup_ts: float = 0.0

    async def _cooldown_left(self) -> Tuple[float, str]:
        async with self._cooldown_lock:
            left = self._cooldown_until_ts - _wall_now()
            return (left if left > 0 else 0.0), self._cooldown_reason

    async def _set_cooldown(self, seconds: int, reason: str) -> None:
        sec = _cap_cooldown(seconds)
        async with self._cooldown_lock:
            self._cooldown_until_ts = _wall_now() + sec
            self._cooldown_reason = reason

    async def _maybe_cleanup(self) -> None:
        """
        Cheap periodic cleanup to prevent unbounded growth.
        """
        now = _wall_now()
        if now - self._last_cleanup_ts < GW_CLEANUP_EVERY_S:
            return
        self._last_cleanup_ts = now

        async with self._cache_lock:
            # remove expired
            keys = list(self._cache.keys())
            for k in keys:
                exp, _ = self._cache.get(k, (0.0, None))
                if exp <= now:
                    self._cache.pop(k, None)

            # cap size (remove oldest exp first)
            if len(self._cache) > GW_MAX_CACHE_ITEMS:
                items = sorted(self._cache.items(), key=lambda kv: kv[1][0])  # by exp_ts
                to_remove = len(self._cache) - GW_MAX_CACHE_ITEMS
                for i in range(max(0, to_remove)):
                    self._cache.pop(items[i][0], None)

    async def run(
        self,
        *,
        user_id: int,
        kind: str,
        cache_key: str,
        prompt: str,
        max_tokens: int,
        call_llm_fn: CallLLMFn,
        ttl_s: int,
    ) -> Tuple[Optional[Any], GatewayMeta]:
        """
        Signature must match llm_client.py usage.

        Returns: (obj_or_none, GatewayMeta)
        """
        t0 = _mono_now()
        await self._maybe_cleanup()

        # 1) cooldown guard
        left, reason = await self._cooldown_left()
        if left > 0.01:
            meta = GatewayMeta(
                provider=self.provider,
                elapsed_ms=int((_mono_now() - t0) * 1000),
                used_fallback=True,
                last_error=f"cooldown_http_429:{left:.1f}s" if reason else f"cooldown:{left:.1f}s",
                cache="miss",
            )
            return None, meta

        # 2) cache hit
        now = _wall_now()
        async with self._cache_lock:
            hit = self._cache.get(cache_key)
            if hit:
                exp_ts, obj = hit
                if exp_ts > now:
                    meta = GatewayMeta(
                        provider=self.provider,
                        elapsed_ms=int((_mono_now() - t0) * 1000),
                        used_fallback=False,
                        last_error=None,
                        cache="hit",
                    )
                    return obj, meta
                else:
                    self._cache.pop(cache_key, None)

        # 3) in-flight dedupe (if same key already being computed)
        async with self._inflight_lock:
            task = self._inflight.get(cache_key)
            if task is not None:
                try:
                    obj, meta = await task
                    # overwrite cache marker
                    meta.cache = "dedupe"
                    meta.elapsed_ms = int((_mono_now() - t0) * 1000)
                    return obj, meta
                except Exception as e:
                    # if the inflight task crashed, fall through and recompute
                    logger.warning("gateway inflight task failed for key=%s err=%r", cache_key, e)

            # create task for this key
            task = asyncio.create_task(
                self._compute_and_cache(
                    cache_key=cache_key,
                    ttl_s=int(ttl_s),
                    call_llm_fn=call_llm_fn,
                    t0=t0,
                )
            )
            self._inflight[cache_key] = task

        try:
            obj, meta = await task
            # task already returns meta.cache="miss"
            meta.elapsed_ms = int((_mono_now() - t0) * 1000)
            return obj, meta
        finally:
            async with self._inflight_lock:
                # remove if still pointing to our task
                cur = self._inflight.get(cache_key)
                if cur is task:
                    self._inflight.pop(cache_key, None)

    async def _compute_and_cache(
        self,
        *,
        cache_key: str,
        ttl_s: int,
        call_llm_fn: CallLLMFn,
        t0: float,
    ) -> Tuple[Optional[Any], GatewayMeta]:
        """
        Calls llm_client's internal _call_llm(), handles 429 cooldown,
        stores cache on success.
        """
        try:
            obj, meta_like, headers = await call_llm_fn()

            # if 429 -> set cooldown
            http_status = int(meta_like.get("http_status") or 0)
            if http_status == 429:
                ra = _parse_retry_after(headers) or LLM_429_COOLDOWN_DEFAULT_S
                await self._set_cooldown(ra, reason="http_429")
                gmeta = GatewayMeta(
                    provider=self.provider,
                    elapsed_ms=int((_mono_now() - t0) * 1000),
                    used_fallback=True,
                    last_error="http_429",
                    cache="miss",
                )
                return None, gmeta

            # if no obj -> pass through
            if obj is None:
                gmeta = GatewayMeta(
                    provider=self.provider,
                    elapsed_ms=int((_mono_now() - t0) * 1000),
                    used_fallback=True,
                    last_error=str(meta_like.get("last_error") or "no_result"),
                    cache="miss",
                )
                return None, gmeta

            # success -> cache
            exp_ts = _wall_now() + max(0, int(ttl_s))
            async with self._cache_lock:
                self._cache[cache_key] = (exp_ts, obj)

            gmeta = GatewayMeta(
                provider=self.provider,
                elapsed_ms=int((_mono_now() - t0) * 1000),
                used_fallback=False,
                last_error=None,
                cache="miss",
            )
            return obj, gmeta

        except Exception as e:
            logger.exception("gateway compute failed key=%s err=%r", cache_key, e)
            gmeta = GatewayMeta(
                provider=self.provider,
                elapsed_ms=int((_mono_now() - t0) * 1000),
                used_fallback=True,
                last_error=f"gateway_exception:{type(e).__name__}",
                cache="miss",
            )
            return None, gmeta


# -----------------------------
# Singleton + API
# -----------------------------
_GATEWAY_SINGLETON: Optional[LLMGateway] = None
_GATEWAY_LOCK = asyncio.Lock()


async def get_gateway() -> LLMGateway:
    """
    llm_client.py делает: gw = await get_gateway()
    Поэтому это async.
    """
    global _GATEWAY_SINGLETON
    if _GATEWAY_SINGLETON is not None:
        return _GATEWAY_SINGLETON

    async with _GATEWAY_LOCK:
        if _GATEWAY_SINGLETON is not None:
            return _GATEWAY_SINGLETON

        provider = "dummy" if LLM_PROVIDER == "dummy" else "openai"
        _GATEWAY_SINGLETON = LLMGateway(provider=provider)
        logger.info("LLM gateway initialized: provider=%s", provider)
        return _GATEWAY_SINGLETON
