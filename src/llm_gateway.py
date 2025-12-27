# src/llm_gateway.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# -----------------------------
# ENV
# -----------------------------
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()  # openai | dummy
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.1").strip())


@dataclass
class GatewayResult:
    obj: Dict[str, Any]
    raw_text: str
    request_id: Optional[str] = None


class BaseGateway:
    """
    Унифицированный слой, чтобы llm_client мог дергать gateway.
    """

    async def chat_json(
        self,
        *,
        domain_prompt: str,
        system_prompt: str,
        timeout_s: float,
        max_tokens: int = 260,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> GatewayResult:
        raise NotImplementedError

    # алиасы на случай несовпадения имён в llm_client
    async def chat_completions_json(self, **kwargs) -> GatewayResult:
        return await self.chat_json(**kwargs)

    async def completion_json(self, **kwargs) -> GatewayResult:
        return await self.chat_json(**kwargs)


class DummyGateway(BaseGateway):
    async def chat_json(self, **kwargs) -> GatewayResult:
        obj = {
            "title": "📊 Обзор рынков",
            "summary": "LLM_PROVIDER=dummy — безопасный режим.",
            "key_factors": ["Сейчас используется заглушка вместо LLM."],
            "line_logic": ["Чтобы включить LLM: LLM_PROVIDER=openai и OPENAI_API_KEY задан."],
            "risks": ["Данные ограничены."],
            "disclaimer": "Аналитический материал, не является рекомендацией.",
        }
        return GatewayResult(obj=obj, raw_text=json.dumps(obj, ensure_ascii=False))


class OpenAIGateway(BaseGateway):
    async def chat_json(
        self,
        *,
        domain_prompt: str,
        system_prompt: str,
        timeout_s: float,
        max_tokens: int = 260,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> GatewayResult:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")

        _model = (model or OPENAI_MODEL).strip()
        _temp = float(OPENAI_TEMPERATURE if temperature is None else temperature)

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": _model,
            "temperature": _temp,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": domain_prompt},
            ],
            "max_tokens": int(max_tokens),
        }

        timeout = httpx.Timeout(
            timeout_s,
            connect=min(8.0, timeout_s),
            read=timeout_s,
            write=min(8.0, timeout_s),
            pool=min(8.0, timeout_s),
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)

            request_id = None
            try:
                request_id = r.headers.get("x-request-id")
            except Exception:
                request_id = None

            # 429/5xx пусть обработает llm_client
            r.raise_for_status()
            data = r.json()

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise ValueError("empty OpenAI response content")

        obj = json.loads(content)
        if not isinstance(obj, dict):
            raise ValueError("model JSON root is not an object")

        return GatewayResult(obj=obj, raw_text=content, request_id=request_id)


# -----------------------------
# Singleton
# -----------------------------
_GATEWAY_SINGLETON: Optional[BaseGateway] = None


def llm_gateway() -> BaseGateway:
    """
    Синхронный доступ к singleton gateway.
    """
    global _GATEWAY_SINGLETON
    if _GATEWAY_SINGLETON is not None:
        return _GATEWAY_SINGLETON

    if LLM_PROVIDER == "dummy":
        _GATEWAY_SINGLETON = DummyGateway()
    else:
        _GATEWAY_SINGLETON = OpenAIGateway()

    logger.info("LLM gateway initialized: provider=%s", LLM_PROVIDER)
    return _GATEWAY_SINGLETON


# ✅ ВАЖНО: llm_client.py делает "await get_gateway()"
async def get_gateway() -> BaseGateway:
    return llm_gateway()


# (опционально) если где-то потребуется синхронный вызов
def get_gateway_sync() -> BaseGateway:
    return llm_gateway()
