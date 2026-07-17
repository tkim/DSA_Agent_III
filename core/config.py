"""
Central configuration for DSA_Agent_III.

Single place that loads `.env` and resolves every runtime setting. Import this
module before anything reads the environment so defaults are consistent across
the CLI, agents, router, and the hybrid model manager.

Transport: Lemonade Server's OpenAI-compatible API (`/v1`). Lemonade is the
*only* runtime — there is no Ollama here. Keep Lemonade on its default port
13305 so it never collides with a separate Ollama install on 11434.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # load .env once, before any os.getenv below
except Exception:  # noqa: BLE001 - dotenv is optional at runtime
    pass


# ---------------------------------------------------------------------------
# Lemonade transport (OpenAI-compatible /v1)
# ---------------------------------------------------------------------------
LEMONADE_BASE_URL = os.getenv("LEMONADE_BASE_URL", "http://localhost:13305/v1")
# The local server ignores the key value, but the openai client requires one.
LEMONADE_API_KEY = os.getenv("LEMONADE_API_KEY", "lemonade")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
# Heavy reasoning + tool-calling model, served on the iGPU (llama.cpp / ROCm).
AGENT_MODEL = os.getenv("AGENT_MODEL", "Qwen3-Coder-30B-A3B-Instruct-GGUF")

# Small model for routing/planning, ideally a Lemonade-managed RyzenAI *Hybrid*
# model on the NPU. Defaults to AGENT_MODEL so the app works before an NPU model
# is pulled (Phase-0 Gate B); set ROUTER_MODEL in .env to the NPU hybrid name.
ROUTER_MODEL = os.getenv("ROUTER_MODEL", AGENT_MODEL)
PLANNER_MODEL = os.getenv("PLANNER_MODEL", ROUTER_MODEL)

# llama.cpp backend for the iGPU model. ROCm is the stable choice on gfx1151
# (Strix Halo); Vulkan is faster but leaks shared memory on this silicon.
LLAMACPP_BACKEND = os.getenv("LLAMACPP_BACKEND", "rocm")

# ---------------------------------------------------------------------------
# Generation defaults
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT_S = float(os.getenv("LEMONADE_TIMEOUT_S", "120"))
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "2048"))
TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.1"))

# ---------------------------------------------------------------------------
# Feature toggles
# ---------------------------------------------------------------------------
# Plan-then-act: NPU model drafts a short plan before the iGPU tool loop.
ENABLE_PLANNER = os.getenv("ENABLE_PLANNER", "0") == "1"
# Omni-modality: spoken answers via Lemonade TTS.
ENABLE_TTS = os.getenv("ENABLE_TTS", "1") == "1"

# Optional Lemonade modality endpoints (confirmed in Phase-0 Gate C).
# Left overridable because exact paths may vary by Lemonade build.
LEMONADE_TTS_URL = os.getenv("LEMONADE_TTS_URL", "")  # e.g. http://localhost:13305/api/v1/audio/speech
TTS_MODEL = os.getenv("TTS_MODEL", "")
TTS_VOICE = os.getenv("TTS_VOICE", "")


def native_base_url() -> str:
    """
    Root URL of the Lemonade server (without the trailing OpenAI `/v1`), used for
    Lemonade-specific management/modality endpoints (`/api/...`).
    """
    base = LEMONADE_BASE_URL
    if base.rstrip("/").endswith("/v1"):
        base = base.rstrip("/")[: -len("/v1")]
    return base.rstrip("/")
