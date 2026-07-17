"""
LLM transport adapter — the single swap point for the inference backend.

DSA_Agent_III talks to Lemonade Server's OpenAI-compatible `/v1` endpoint via the
official `openai` client. Everything upstream (agents, router, planner) consumes
the normalized `LLMResponse` below and never sees the raw wire shape — so moving
to Lemonade's Ollama-compat (`/api/chat`) or Anthropic (`/v1/messages`) surface
would touch only this file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from core import config


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


class LLMClient:
    """Thin, normalized wrapper over Lemonade's OpenAI-compatible chat API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._client = OpenAI(
            base_url=base_url or config.LEMONADE_BASE_URL,
            api_key=api_key or config.LEMONADE_API_KEY,
            timeout=config.REQUEST_TIMEOUT_S,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": config.TEMPERATURE if temperature is None else temperature,
            "max_tokens": config.MAX_TOKENS if max_tokens is None else max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(**kwargs)
        return self._normalize(resp)

    @staticmethod
    def _normalize(resp: Any) -> LLMResponse:
        msg = resp.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            raw_args = tc.function.arguments
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            else:
                args = raw_args or {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        return LLMResponse(content=msg.content or "", tool_calls=tool_calls, raw=resp)
