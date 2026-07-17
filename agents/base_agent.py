"""
Base agent: tool-calling loop with RAG context injection.

Runs on Lemonade's OpenAI-compatible transport via `core.llm_client.LLMClient`.
The tool schemas (`TOOL_SCHEMAS`) are already OpenAI function-calling format, so
they are passed through unchanged.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod

from core import config
from core.llm_client import LLMClient
from rag.retriever import retrieve

MAX_ITERATIONS = 8

# Shared answering policy appended to every agent's system prompt.
#
# Instruction-following models take "use the registered tools" literally and
# refuse conceptual questions ("none of the provided functions can be used")
# unless explicitly told that answering from knowledge is allowed. Most platform
# questions are conceptual and have no matching tool.
#
# No literal braces in here: base_agent formats system_template with str.format().
ANSWERING_POLICY = (
    "How to answer:\n"
    "1. If a registered tool can answer the request, call it immediately. "
    "Do not describe what you would do.\n"
    "2. If no tool fits, that is normal and expected. Answer from the documentation "
    "context below plus your own knowledge. Never refuse a question merely because "
    "no function matches it, and never reply that the available functions are "
    "irrelevant - just answer the question.\n"
    "3. Prefer the documentation context when it is relevant, and cite the source "
    "path you used.\n"
    "4. If the context is empty or unrelated, answer from your own knowledge and say "
    "plainly that it is not backed by the local docs.\n"
    "5. After each tool result, incorporate it before deciding the next action. "
    "Report tool errors verbatim.\n"
    "6. Never invent cluster IDs, table names, policy ARNs, SQL results, or metric "
    "values. Those must come from a tool.\n"
)


class BaseAgent(ABC):
    platform: str
    tool_schemas: list
    tool_executors: dict
    system_template: str   # must contain {rag_context} and {answering_policy}

    def __init__(self, model: str):
        self.model = model
        self.client = LLMClient()
        self.register_tools()

    @abstractmethod
    def register_tools(self):
        pass

    def run(self, query: str, history: list | None = None) -> dict:
        t0 = time.time()
        tool_log: list[dict] = []

        rag_results = retrieve(self.platform, query)
        rag_context = self._fmt_rag(rag_results)
        rag_sources = [{"source": r["source"], "score": r["score"]} for r in rag_results]

        system_content = self.system_template.format(
            rag_context=rag_context,
            answering_policy=ANSWERING_POLICY,
        )

        # Optional plan-then-act: a small NPU model drafts a short plan first.
        if config.ENABLE_PLANNER:
            plan = self._maybe_plan(query)
            if plan:
                system_content += f"\n\nSuggested approach:\n{plan}"

        messages = (
            [{"role": "system", "content": system_content}]
            + (history or [])
            + [{"role": "user", "content": query}]
        )

        for _ in range(MAX_ITERATIONS):
            resp = self._llm(messages)

            if not resp.tool_calls:
                return {
                    "response":        resp.content or "",
                    "tool_calls_made": tool_log,
                    "rag_sources":     rag_sources,
                    "latency_ms":      int((time.time() - t0) * 1000),
                }

            # Echo the assistant's tool-call turn back into the conversation
            # (OpenAI protocol: assistant message carrying tool_calls).
            messages.append({
                "role": "assistant",
                "content": resp.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in resp.tool_calls
                ],
            })
            for tc in resp.tool_calls:
                result = self._run_tool(tc.name, tc.args)
                tool_log.append({"name": tc.name, "args": tc.args, "result": result})
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps(result, default=str),
                })

        return {
            "response":        "Max iterations reached. See tool_calls_made for partial results.",
            "tool_calls_made": tool_log,
            "rag_sources":     rag_sources,
            "latency_ms":      int((time.time() - t0) * 1000),
        }

    def _llm(self, messages):
        return self.client.chat(
            model=self.model,
            messages=messages,
            tools=self.tool_schemas,
        )

    def _maybe_plan(self, query: str) -> str:
        try:
            from agents.planner import draft_plan
            return draft_plan(query, self.platform)
        except Exception:  # noqa: BLE001 - planning is optional, never fatal
            return ""

    def _run_tool(self, name: str, args: dict) -> dict:
        fn = self.tool_executors.get(name)
        if not fn:
            return {"error": f"No executor registered for tool: {name}"}
        try:
            return fn(**args)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "tool": name}

    def _fmt_rag(self, results: list) -> str:
        if not results:
            return "No relevant documentation retrieved."
        return "\n".join(
            f"[Source: {r['source']} | Score: {r['score']:.2f}]\n{r['content'].strip()}\n"
            for r in results[:5]
        )
