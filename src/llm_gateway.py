from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---- Redis client (redis-py asyncio) ----
# pip: redis>=5
try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore


@dataclass
class RateLimits:
    # per minute budgets
    rpm: int
    tpm: int
    # per user per minute budgets
    user_rpm: int
    user_tpm: int


def _now_s() -> float:
    return time.time()


def _minute_bucket(ts: Optional[float] = None) -> int:
    t = int(ts or _now_s())
    return t // 60


def _estimate_tokens(text: str) -> int:
    """
    Быстрая оценка токенов без tiktoken:
    - грубо 1 токен ~ 3.5-4 символа латиницы
    - для кириллицы примерно похоже
    """
    s = text or ""
    n = len(s)
    # минимум 1
    return max(1, int(n / 4) + 1)


def _hash_key(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class LLMGateway:
    """
    Gateway responsibilities:
    - global & per-user rate-limit (RPM/TPM)
    - cache (Redis)
    - in-flight de-duplication (singleflight) to avoid N parallel calls
    - cooldown handling after 429
    """

    def __init__(self) -> None:
        self.enabled = (os.getenv("LLM_GATEWAY_ENABLED") or "1").strip() == "1"

        self.redis_url = (os.getenv("REDIS_URL") or "").strip()
        self.prefix = (os.getenv("LLM_REDIS_PREFIX") or "khl:llm").strip()

        self.cache_ttl_pre = int((os.getenv("LLM_CACHE_TTL_S") or "900").strip())
        self.cache_ttl_live = int((os.getenv("LLM_CACHE_TTL_LIVE_S") or "20").strip())

        # singleflight lock TTL: enough to cover request time
        self.lock_ttl_s = float((os.getenv("LLM_SINGLEFLIGHT_TTL_S") or "15").strip())

        # soft cooldown on 429 (if Retry-After huge, we still clamp for UX)
        self.cooldown_cap_s = int((os.getenv("LLM_COOLDOWN_CAP_S") or "600").strip())

        # ---- Default limits: safe for Telegram ----
        # Your OpenAI headers show: RPM 200, TPM 100000 for model (org-level)
        # We keep headroom so web/ops don't die:
        # global: 60 rpm, 20k tpm by default (can tune)
        self.free = RateLimits(
            rpm=int((os.getenv("LLM_FREE_RPM") or "40").strip()),
            tpm=int((os.getenv("LLM_FREE_TPM") or "12000").strip()),
            user_rpm=int((os.getenv("LLM_FREE_USER_RPM") or "6").strip()),
            user_tpm=int((os.getenv("LLM_FREE_USER_TPM") or "2500").strip()),
        )
        self.premium = RateLimits(
            rpm=int((os.getenv("LLM_PREMIUM_RPM") or "120").strip()),
            tpm=int((os.getenv("LLM_PREMIUM_TPM") or "40000").strip()),
            user_rpm=int((os.getenv("LLM_PREMIUM_USER_RPM") or "20").strip()),
            user_tpm=int((os.getenv("LLM_PREMIUM_USER_TPM") or "8000").strip()),
        )

        self._r: Optional["redis.Redis"] = None
        self._local_fallback_lock = asyncio.Lock()
        self._local_cache: Dict[str, Tuple[float, Any, dict]] = {}  # fallback if no redis

    async def _get_redis(self) -> Optional["redis.Redis"]:
        if not self.enabled:
            return None
        if not self.redis_url:
            return None
        if redis is None:
            logger.warning("redis.asyncio is not available; gateway will use local fallback.")
            return None
        if self._r is None:
            self._r = redis.from_url(self.redis_url, decode_responses=True)
        return self._r

    # -----------------------
    # Cooldown
    # -----------------------
    def _cooldown_key(self, model: str) -> str:
        return f"{self.prefix}:cooldown:{model}"

    async def set_cooldown(self, model: str, seconds: int) -> None:
        r = await self._get_redis()
        seconds = max(1, min(int(seconds), int(self.cooldown_cap_s)))
        until = int(_now_s() + seconds)
        if r:
            await r.set(self._cooldown_key(model), str(until), ex=seconds)
        else:
            # local fallback: store in local cache with special key
            async with self._local_fallback_lock:
                self._local_cache[self._cooldown_key(model)] = (until, {"until": until}, {"_cooldown": True})

    async def get_cooldown_remaining(self, model: str) -> float:
        now = _now_s()
        r = await self._get_redis()
        if r:
            v = await r.get(self._cooldown_key(model))
            if not v:
                return 0.0
            try:
                until = int(v)
            except Exception:
                return 0.0
            return max(0.0, float(until) - now)
        else:
            async with self._local_fallback_lock:
                hit = self._local_cache.get(self._cooldown_key(model))
                if not hit:
                    return 0.0
                exp_ts, _, meta = hit
                if meta.get("_cooldown"):
                    return max(0.0, float(exp_ts) - now)
            return 0.0

    # -----------------------
    # Rate limiting
    # -----------------------
    def _rate_keys(self, tier: str, model: str, user_id: int, bucket: int) -> Dict[str, str]:
        base = f"{self.prefix}:rate:{tier}:{model}:{bucket}"
        return {
            "g_rpm": f"{base}:g_rpm",
            "g_tpm": f"{base}:g_tpm",
            "u_rpm": f"{base}:u:{user_id}:rpm",
            "u_tpm": f"{base}:u:{user_id}:tpm",
        }

    async def _incr_with_ttl(self, r: "redis.Redis", key: str, inc: int, ttl_s: int) -> int:
        pipe = r.pipeline()
        pipe.incrby(key, inc)
        pipe.expire(key, ttl_s)
        res = await pipe.execute()
        return int(res[0])

    async def check_and_consume(
        self,
        *,
        tier: str,
        model: str,
        user_id: int,
        est_tokens: int,
    ) -> Tuple[bool, str]:
        limits = self.premium if tier == "premium" else self.free
        bucket = _minute_bucket()
        ttl_s = 75  # keep bucket keys slightly longer than 60 sec

        r = await self._get_redis()
        if not r:
            # local fallback (rough)
            return True, "ok_local"

        keys = self._rate_keys(tier, model, user_id, bucket)

        # Read current counters
        pipe = r.pipeline()
        pipe.get(keys["g_rpm"])
        pipe.get(keys["g_tpm"])
        pipe.get(keys["u_rpm"])
        pipe.get(keys["u_tpm"])
        got = await pipe.execute()

        def _int(x: Any) -> int:
            try:
                return int(x or 0)
            except Exception:
                return 0

        g_rpm = _int(got[0])
        g_tpm = _int(got[1])
        u_rpm = _int(got[2])
        u_tpm = _int(got[3])

        if g_rpm + 1 > limits.rpm:
            return False, "rate_global_rpm"
        if g_tpm + est_tokens > limits.tpm:
            return False, "rate_global_tpm"
        if u_rpm + 1 > limits.user_rpm:
            return False, "rate_user_rpm"
        if u_tpm + est_tokens > limits.user_tpm:
            return False, "rate_user_tpm"

        # Consume
        await self._incr_with_ttl(r, keys["g_rpm"], 1, ttl_s)
        await self._incr_with_ttl(r, keys["g_tpm"], est_tokens, ttl_s)
        await self._incr_with_ttl(r, keys["u_rpm"], 1, ttl_s)
        await self._incr_with_ttl(r, keys["u_tpm"], est_tokens, ttl_s)
        return True, "ok"

    # -----------------------
    # Cache + singleflight
    # -----------------------
    def _cache_key(self, schema: str, cache_key: str) -> str:
        return f"{self.prefix}:cache:{schema}:{cache_key}"

    def _lock_key(self, schema: str, cache_key: str) -> str:
        return f"{self.prefix}:lock:{schema}:{cache_key}"

    async def cache_get(self, schema: str, cache_key: str) -> Optional[dict]:
        r = await self._get_redis()
        if r:
            v = await r.get(self._cache_key(schema, cache_key))
            if not v:
                return None
            try:
                return json.loads(v)
            except Exception:
                return None

        # local fallback
        now = _now_s()
        async with self._local_fallback_lock:
            hit = self._local_cache.get(self._cache_key(schema, cache_key))
            if not hit:
                return None
            exp_ts, obj, meta = hit
            if exp_ts > now and isinstance(obj, dict) and meta.get("_is_cache"):
                return obj
            self._local_cache.pop(self._cache_key(schema, cache_key), None)
        return None

    async def cache_set(self, schema: str, cache_key: str, obj: dict, ttl_s: int) -> None:
        r = await self._get_redis()
        if r:
            await r.set(self._cache_key(schema, cache_key), json.dumps(obj, ensure_ascii=False), ex=int(ttl_s))
            return
        async with self._local_fallback_lock:
            self._local_cache[self._cache_key(schema, cache_key)] = (_now_s() + ttl_s, obj, {"_is_cache": True})

    async def acquire_lock(self, schema: str, cache_key: str) -> bool:
        r = await self._get_redis()
        if r:
            # SET key value NX EX
            return bool(await r.set(self._lock_key(schema, cache_key), "1", nx=True, ex=int(self.lock_ttl_s)))
        # local fallback lock: always allow (not perfect, but ok)
        return True

    async def release_lock(self, schema: str, cache_key: str) -> None:
        r = await self._get_redis()
        if r:
            await r.delete(self._lock_key(schema, cache_key))

    async def wait_for_cache(
        self,
        schema: str,
        cache_key: str,
        timeout_s: float = 6.0,
        poll_s: float = 0.15,
    ) -> Optional[dict]:
        deadline = _now_s() + max(0.1, timeout_s)
        while _now_s() < deadline:
            obj = await self.cache_get(schema, cache_key)
            if obj is not None:
                return obj
            await asyncio.sleep(poll_s)
        return None

    # -----------------------
    # Public: execute
    # -----------------------
    async def execute(
        self,
        *,
        user_id: int,
        tier: str,
        model: str,
        schema: str,
        cache_key: str,
        ttl_s: int,
        prompt_for_tokens: str,
        call_fn,  # async () -> dict
        wait_timeout_s: float = 7.0,
    ) -> Tuple[dict, dict]:
        """
        Returns: (obj, meta)
        - obj is dict (response content parsed) or fallback dict (caller decides)
        - meta includes: cache hit/miss, used_fallback, last_error, provider, cooldown
        """
        # cooldown check
        cd = await self.get_cooldown_remaining(model)
        if cd > 0.2:
            return {}, {
                "provider": "openai",
                "cache": "miss",
                "used_fallback": True,
                "last_error": f"cooldown_http_429:{cd:.1f}s",
                "elapsed_ms": 0,
            }

        # cache hit?
        cached = await self.cache_get(schema, cache_key)
        if cached is not None:
            return cached, {
                "provider": "openai",
                "cache": "hit",
                "used_fallback": False,
                "last_error": None,
                "elapsed_ms": 0,
            }

        est = _estimate_tokens(prompt_for_tokens)
        ok, reason = await self.check_and_consume(tier=tier, model=model, user_id=user_id, est_tokens=est)
        if not ok:
            # rate limited locally (product rule), caller will fallback
            return {}, {
                "provider": "openai",
                "cache": "miss",
                "used_fallback": True,
                "last_error": f"gateway_rate_limited:{reason}",
                "elapsed_ms": 0,
            }

        # singleflight: only one request in-flight per cache_key
        got_lock = await self.acquire_lock(schema, cache_key)
        if not got_lock:
            # someone else is generating it; wait for cache
            waited = await self.wait_for_cache(schema, cache_key, timeout_s=wait_timeout_s)
            if waited is not None:
                return waited, {
                    "provider": "openai",
                    "cache": "hit_after_wait",
                    "used_fallback": False,
                    "last_error": None,
                    "elapsed_ms": 0,
                }
            # if still nothing - allow caller to execute (best-effort)
            got_lock = True

        start = time.monotonic()
        try:
            obj = await call_fn()
            if isinstance(obj, dict):
                await self.cache_set(schema, cache_key, obj, ttl_s=int(ttl_s))
            return obj if isinstance(obj, dict) else {}, {
                "provider": "openai",
                "cache": "miss",
                "used_fallback": False,
                "last_error": None,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
            }
        finally:
            try:
                await self.release_lock(schema, cache_key)
            except Exception:
                pass


# singleton
_GATEWAY = LLMGateway()


def get_llm_gateway() -> LLMGateway:
    return _GATEWAY
