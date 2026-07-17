"""
Optional plan-then-act step.

When ENABLE_PLANNER=1, a small model (PLANNER_MODEL, ideally the NPU RyzenAI
hybrid) drafts a short numbered plan before the iGPU coder runs its tool loop.
This addresses DSA_Agent_II's documented gap (purely reactive loop, no
plan-then-act) while keeping the main loop untouched — the plan is injected as a
hint into the system prompt.

Kept deliberately cheap and best-effort: any failure returns "" and the agent
proceeds exactly as before.
"""
from __future__ import annotations

from core import config
from core.llm_client import LLMClient

_PLAN_PROMPT = (
    "You are a planning assistant for a {platform} infrastructure agent. "
    "Given the user's request, write a concise numbered plan (max 4 steps) of how "
    "to answer it — which tools to call and in what order, or that it is a "
    "conceptual question to answer directly. Output only the plan.\n\n"
    "Request: {query}\nPlan:"
)

# Module-level client so the planner reuses one connection.
_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def draft_plan(query: str, platform: str) -> str:
    """Return a short plan string, or '' if planning is disabled or fails."""
    if not config.ENABLE_PLANNER:
        return ""
    try:
        resp = _get_client().chat(
            model=config.PLANNER_MODEL,
            messages=[{"role": "user",
                       "content": _PLAN_PROMPT.format(platform=platform, query=query)}],
            temperature=0.0,
            max_tokens=256,
        )
        return (resp.content or "").strip()
    except Exception:  # noqa: BLE001 - planning must never break a query
        return ""
