"""
Local LLM client (Ollama). Nothing here ever reaches the internet: the URL is
the local Ollama daemon, and the model receives only masked text.
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMUnavailable(Exception):
    pass


class LLM(Protocol):
    model: str

    async def chat_json(self, system: str, user: str, schema: dict) -> dict: ...
    async def chat_text(self, system: str, user: str, max_tokens: int = 200) -> str: ...
    async def health(self) -> bool: ...


class OllamaLLM:
    def __init__(self, base_url: str, model: str, timeout: float = 45) -> None:
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def _chat(self, messages: list[dict], **extra: Any) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,          # Qwen3: skip the reasoning trace -> lower latency
            "keep_alive": "30m",     # keep weights resident on the GPU between questions
            "options": {"temperature": 0, "num_ctx": 8192, **extra.pop("options", {})},
            **extra,
        }
        try:
            r = await self._client.post("/api/chat", json=payload)
            r.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise LLMUnavailable(str(exc)) from exc
        content = r.json().get("message", {}).get("content", "")
        return _THINK.sub("", content).strip()

    async def chat_json(self, system: str, user: str, schema: dict) -> dict:
        raw = await self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            format=schema,   # Ollama structured output: constrained to this JSON schema
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"model returned non-JSON: {raw[:120]!r}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable("model returned non-object JSON")
        return data

    async def chat_text(self, system: str, user: str, max_tokens: int = 200) -> str:
        return await self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            options={"num_predict": max_tokens},
        )

    async def health(self) -> bool:
        try:
            r = await self._client.get("/api/tags", timeout=3)
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
            return any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                       for n in names)
        except (httpx.HTTPError, ValueError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
