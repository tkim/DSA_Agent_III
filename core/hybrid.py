"""
Hybrid model manager — the iGPU + NPU scheduler.

Owns which silicon serves which model:
  - AGENT_MODEL   -> iGPU (llama.cpp / ROCm): heavy reasoning + tool-calling
  - ROUTER_MODEL  -> NPU (RyzenAI hybrid):    fast routing/planning

Lemonade loads a model on first request to it, so `warm()` simply issues a tiny
completion to each configured model to pull them into memory ahead of the first
real query — the backend-agnostic, reliable way to preload (no dependency on
Lemonade's load-endpoint schema). It also keeps both models resident so the NPU
router and iGPU coder can run concurrently (verified in Phase-0 Gate B).

`status()` reports what is loaded for the CLI `/models` line; it degrades to the
configured names if the server's introspection endpoint is unavailable.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from core import config
from core.llm_client import LLMClient


class HybridManager:
    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    # -- model selection -----------------------------------------------------
    @property
    def agent_model(self) -> str:
        return config.AGENT_MODEL

    @property
    def router_model(self) -> str:
        return config.ROUTER_MODEL

    @property
    def planner_model(self) -> str:
        return config.PLANNER_MODEL

    def _models_to_warm(self) -> list[str]:
        # dedupe while preserving order (router/planner may equal agent model)
        seen: dict[str, None] = {}
        for m in (config.AGENT_MODEL, config.ROUTER_MODEL, config.PLANNER_MODEL):
            if m:
                seen.setdefault(m, None)
        return list(seen)

    # -- lifecycle -----------------------------------------------------------
    def warm(self) -> dict[str, bool]:
        """
        Preload each configured model into memory with a 1-token completion.
        Best-effort: a failure (e.g. NPU model not pulled yet) is reported, not
        raised, so the CLI can still start.
        """
        results: dict[str, bool] = {}
        for model in self._models_to_warm():
            try:
                self.client.chat(
                    model=model,
                    messages=[{"role": "user", "content": "ok"}],
                    max_tokens=1,
                    temperature=0.0,
                )
                results[model] = True
            except Exception:  # noqa: BLE001 - warming is best-effort
                results[model] = False
        return results

    def status(self) -> list[dict]:
        """
        Return [{name, role, loaded}] for the /models CLI line. Tries Lemonade's
        Ollama-compatible /api/ps for the live loaded set; falls back to the
        configured model list marked as unknown-loaded.
        """
        roles = {
            config.AGENT_MODEL: "iGPU coder",
            config.ROUTER_MODEL: "NPU router",
            config.PLANNER_MODEL: "NPU planner",
        }
        loaded_names = self._loaded_names()
        out: list[dict] = []
        for name in self._models_to_warm():
            out.append({
                "name": name,
                "role": roles.get(name, "model"),
                "loaded": (name in loaded_names) if loaded_names is not None else None,
            })
        return out

    def _loaded_names(self) -> set[str] | None:
        """Best-effort query of currently-loaded models; None if unavailable."""
        url = f"{config.native_base_url()}/api/ps"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                import json
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        models = data.get("models") or data.get("data") or []
        names: set[str] = set()
        for m in models:
            n = m.get("name") or m.get("model") or m.get("id")
            if n:
                names.add(n)
        return names
