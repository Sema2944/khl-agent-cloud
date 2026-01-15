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
# NEW: overload controls
# -----------------------------
# Concurrency: how many LLM calls at once (process-local)
GW_MAX_CONCURRENCY_TOTAL = int((os.getenv("GW_MAX_CONCURRENCY_TOTAL") or "4").strip())
GW_MAX_CONCURRENCY_PRE = int((os.getenv("GW_MAX_CONCURRENCY_PRE") or "3").strip())
GW_MAX_CONCURRENCY_LIVE = int((os.getenv("GW_MAX_CONCURRENCY_LIVE") or "2").strip())

# RPS throttles (process-local, simple token bucket)
GW_RPS_PRE = float((os.getenv("GW_RPS_PRE") or "1.0").strip())     # 1 req/sec
GW_RPS_LIVE = float((os.getenv("GW_RPS_LIVE") or "2.0").strip())   # 2 req/sec
GW_RPS_BURST = float((os.getenv("GW_RPS_BURST") or "2.0").strip()) # burst capacity multiplier

# SWR: allow serving stale cache for this window after expiry (seconds)
GW_STALE_IF_ERROR_S = int((os.getenv("GW_STALE_IF_ERROR_S") or "3600").strip())  # 1 hour fallback window
GW_STALE_WHILE_REVALIDATE_S = int((os.getenv("GW_STALE_WHILE_REVALIDATE_S") or "300").strip())  # 5 min SWR window


# -----------------------------
# Types expected by llm_client
# -----------------------------
@dataclass
class GatewayMeta:
    provider: str
    elapsed_ms: int
    used_fallback: bool
    last_error: Optional[str]
    cache: str  # "hit" | "miss" | "dedupe" | "stale"


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


@dataclass
class _CacheEntry:
    exp_ts: float          # TTL expiry
    stale_until_ts: float  # allowed stale until (SWR + stale-if-error)
    obj: Any


class _TokenBucket:
    """
    Simple token bucket to limit RPS (process-local).
    """
    def __init__(self, rps: float, burst_mult: float = 2.0):
        self.rps = max(0.0, float(rps))
        self.capacity = max(1.0, float(rps) * max(1.0, float(burst_mult)))
        self.tokens = self.capacity
        self.updated = _mono_now()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rps <= 0:
            return
        async with self._lock:
            while True:
                now = _mono_now()
                elapsed = now - self.updated
                self.updated = now
                # refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return

                # need to wait for next token
                need = 1.0 - self.tokens
                wait_s = need / self.rps if self.rps > 0 else 0.2
                wait_s = min(max(wait_s, 0.01), 0.75)
                await asyncio.sleep(wait_s)


# -----------------------------
# In-memory gateway (MVP+)
# -----------------------------
class LLMGateway:
    """
    Gateway:
    - cache (process-local) with SWR + stale-if-error
    - in-flight dedupe (same cache_key)
    - global cooldown after 429
    - concurrency limits (total + per kind)
    - RPS throttles per kind
    """

    def __init__(self, provider: str):
        self.provider = provider

        # cache_key -> CacheEntry
        self._cache: Dict[str, _CacheEntry] = {}
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

        # concurrency controls
        self._sem_total = asyncio.Semaphore(max(1, GW_MAX_CONCURRENCY_TOTAL))
        self._sem_pre = asyncio.Semaphore(max(1, GW_MAX_CONCURRENCY_PRE))
        self._sem_live = asyncio.Semaphore(max(1, GW_MAX_CONCURRENCY_LIVE))

        # rps controls
        self._tb_pre = _TokenBucket(GW_RPS_PRE, burst_mult=GW_RPS_BURST)
        self._tb_live = _TokenBucket(GW_RPS_LIVE, burst_mult=GW_RPS_BURST)

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
            # remove entries whose stale window ended
            keys = list(self._cache.keys())
            for k in keys:
                e = self._cache.get(k)
                if not e:
                    continue
                if e.stale_until_ts <= now:
                    self._cache.pop(k, None)

            # cap size (remove oldest stale_until first)
            if len(self._cache) > GW_MAX_CACHE_ITEMS:
                items = sorted(self._cache.items(), key=lambda kv: kv[1].stale_until_ts)
                to_remove = len(self._cache) - GW_MAX_CACHE_ITEMS
                for i in range(max(0, to_remove)):
                    self._cache.pop(items[i][0], None)

    def _sem_for_kind(self, kind: str) -> asyncio.Semaphore:
        return self._sem_live if (kind or "").lower() == "live" else self._sem_pre

    def _tb_for_kind(self, kind: str) -> _TokenBucket:
        return self._tb_live if (kind or "").lower() == "live" else self._tb_pre

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

        kind_norm = (kind or "pre").lower()

        # 1) cooldown guard
        left, reason = await self._cooldown_left()
        if left > 0.01:
            # If we have stale cache, serve it instead of fallback
            stale_obj = await self._get_stale_if_any(cache_key)
            if stale_obj is not None:
                meta = GatewayMeta(
                    provider=self.provider,
                    elapsed_ms=int((_mono_now() - t0) * 1000),
                    used_fallback=False,
                    last_error=None,
                    cache="stale",
                )
                return stale_obj, meta

            meta = GatewayMeta(
                provider=self.provider,
                elapsed_ms=int((_mono_now() - t0) * 1000),
                used_fallback=True,
                last_error=f"cooldown_http_429:{left:.1f}s" if reason else f"cooldown:{left:.1f}s",
                cache="miss",
            )
            return None, meta

        # 2) cache hit (fresh)
        now = _wall_now()
        async with self._cache_lock:
            e = self._cache.get(cache_key)
            if e and e.exp_ts > now:
                meta = GatewayMeta(
                    provider=self.provider,
                    elapsed_ms=int((_mono_now() - t0) * 1000),
                    used_fallback=False,
                    last_error=None,
                    cache="hit",
                )
                return e.obj, meta

        # 2.5) SWR: if expired but still within stale window -> return stale immediately,
        # and trigger refresh in background (deduped).
        async with self._cache_lock:
            e = self._cache.get(cache_key)
            if e and (e.exp_ts <= now) and (e.stale_until_ts > now):
                # schedule refresh (single-flight)
                await self._ensure_inflight_refresh(
                    kind=kind_norm,
                    cache_key=cache_key,
                    ttl_s=int(ttl_s),
                    call_llm_fn=call_llm_fn,
                    t0=t0,
                )
                meta = GatewayMeta(
                    provider=self.provider,
                    elapsed_ms=int((_mono_now() - t0) * 1000),
                    used_fallback=False,
                    last_error=None,
                    cache="stale",
                )
                return e.obj, meta

        # 3) in-flight dedupe (if same key already being computed)
        async with self._inflight_lock:
            task = self._inflight.get(cache_key)
            if task is not None:
                try:
                    obj, meta = await task
                    meta.cache = "dedupe"
                    meta.elapsed_ms = int((_mono_now() - t0) * 1000)
                    return obj, meta
                except Exception as e2:
                    logger.warning("gateway inflight task failed for key=%s err=%r", cache_key, e2)

            # create task for this key
            task = asyncio.create_task(
                self._compute_and_cache(
                    kind=kind_norm,
                    cache_key=cache_key,
                    ttl_s=int(ttl_s),
                    call_llm_fn=call_llm_fn,
                    t0=t0,
                )
            )
            self._inflight[cache_key] = task

        try:
            obj, meta = await task
            meta.elapsed_ms = int((_mono_now() - t0) * 1000)

            # If compute failed and we have stale cache, serve stale (stale-if-error)
            if obj is None:
                stale_obj = await self._get_stale_if_any(cache_key)
                if stale_obj is not None:
                    meta.used_fallback = False
                    meta.last_error = None
                    meta.cache = "stale"
                    return stale_obj, meta

            return obj, meta
        finally:
            async with self._inflight_lock:
                cur = self._inflight.get(cache_key)
                if cur is task:
                    self._inflight.pop(cache_key, None)

    async def _get_stale_if_any(self, cache_key: str) -> Optional[Any]:
        now = _wall_now()
        async with self._cache_lock:
            e = self._cache.get(cache_key)
            if e and e.stale_until_ts > now:
                return e.obj
        return None

    async def _ensure_inflight_refresh(
        self,
        *,
        kind: str,
        cache_key: str,
        ttl_s: int,
        call_llm_fn: CallLLMFn,
        t0: float,
    ) -> None:
        # create refresh if not already inflight
        async with self._inflight_lock:
            if cache_key in self._inflight:
                return
            task = asyncio.create_task(
                self._compute_and_cache(
                    kind=kind,
                    cache_key=cache_key,
                    ttl_s=int(ttl_s),
                    call_llm_fn=call_llm_fn,
                    t0=t0,
                )
            )
            self._inflight[cache_key] = task

    async def _compute_and_cache(
        self,
        *,
        kind: str,
        cache_key: str,
        ttl_s: int,
        call_llm_fn: CallLLMFn,
        t0: float,
    ) -> Tuple[Optional[Any], GatewayMeta]:
        """
        Calls llm_client's internal _call_llm(), handles:
        - concurrency limits + rps
        - 429 cooldown
        - stores cache on success (and extends stale window)
        """
        # Acquire concurrency + rps
        sem_kind = self._sem_for_kind(kind)
        tb = self._tb_for_kind(kind)

        await tb.acquire()
        async with self._sem_total:
            async with sem_kind:
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

                    # success -> cache with SWR window
                    now = _wall_now()
                    exp_ts = now + max(0, int(ttl_s))

                    # stale window:
                    # - allow SWR for short time after expiry
                    # - and allow stale-if-error longer (so we can serve something during outages)
                    stale_until_ts = max(
                        exp_ts + max(0, int(GW_STALE_WHILE_REVALIDATE_S)),
                        now + max(0, int(GW_STALE_IF_ERROR_S)),
                    )

                    async with self._cache_lock:
                        self._cache[cache_key] = _CacheEntry(exp_ts=exp_ts, stale_until_ts=stale_until_ts, obj=obj)

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
        logger.info(
            "LLM gateway initialized: provider=%s conc_total=%s conc_pre=%s conc_live=%s rps_pre=%s rps_live=%s swr=%ss stale_if_error=%ss",
            provider,
            GW_MAX_CONCURRENCY_TOTAL,
            GW_MAX_CONCURRENCY_PRE,
            GW_MAX_CONCURRENCY_LIVE,
            GW_RPS_PRE,
            GW_RPS_LIVE,
            GW_STALE_WHILE_REVALIDATE_S,
            GW_STALE_IF_ERROR_S,
        )
        return _GATEWAY_SINGLETON
