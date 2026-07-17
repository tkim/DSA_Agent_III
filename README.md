# DSA Agent III

An AMD-native, fully-local infrastructure agent for **Databricks**, **Snowflake**, and
**AWS** — built on **AMD Lemonade Server 11.0**, running the LLM on the **iGPU** with a
small **NPU** hybrid model for routing/planning, on an Asus Z13 (Ryzen AI Max+ 395 /
Strix Halo).

Successor to [DSA_Agent_II](../DSA_Agent_II). **No Ollama** — Lemonade is the only runtime.
III shares nothing with II except the GPU, so the two run side by side.

---

## Why III (vs II)

| | DSA_Agent_II | **DSA_Agent_III** |
|---|---|---|
| Runtime | Ollama (:11434) | **Lemonade 11.0 (:13305)** |
| Transport | ollama client | **OpenAI `/v1`** (`openai` SDK) |
| Silicon | iGPU only | **iGPU coder + NPU hybrid router** |
| Modality | text | text + **TTS** (`/speak`), optional image/3D |
| Models from | Ollama registry | **Hugging Face** (Lemonade default) |
| Bonus | — | same model also served on **Anthropic `/v1/messages`** for Claude Code |

Raw tok/s is roughly par with II (ROCm ~38, Vulkan ~46 vs Ollama ~48). The wins are the
NPU hybrid, omni-modality, and standard multi-API access — not speed.

---

## Coexistence with DSA_Agent_II — nothing breaks

Because III uses no Ollama, the only shared resource is the iGPU/VRAM.

| Resource | II | III |
|---|---|---|
| Server / port | Ollama :11434 | Lemonade :13305 |
| Client dep | `ollama` | `openai` |
| History DB | `~/.dsa_agent/` | `~/.dsa_agent_iii/` |
| RAG corpus | `II/rag/chroma_db` | `III/rag/chroma_db` |

> Keep Lemonade on **:13305** (it *can* be told to bind Ollama's 11434 — don't). Under
> heavy use, don't keep both 30B models hot at once; they share the 96 GB VGM.

---

## Setup

### Phase 0 — install Lemonade & prove tool-calling (gates, do first)

```powershell
.\infra\00_check_hardware.ps1          # GPU / NPU / VGM / Lemonade reachable
.\infra\01_install_lemonade.ps1        # installs Lemonade 11.0 service on :13305
.\infra\02_pull_models.ps1             # pulls the 30B coder + an NPU hybrid model (from HF)
python .\infra\03_verify_toolcall.py   # GATE A: must pass 3/3 (OpenAI /v1 tool-calling)
.\infra\04_benchmark.ps1               # record iGPU tok/s (and -Model <hybrid> for NPU)
```

If Gate A's tool-calling is weak, the plan's documented fallbacks (Ollama-compat
`/api/chat`, Anthropic `/v1/messages`) are isolated to `core/llm_client.py`.

### Phase 1 — Python env

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -e .
copy .env.example .env                 # set ROUTER_MODEL to your pulled NPU hybrid model
.\.venv\Scripts\python.exe -m pytest tests\ -q    # offline mock suite
```

### Phase 2 — build the RAG corpus (one-time)

```powershell
.\rag\fetch_docs.ps1
.\.venv\Scripts\python.exe -m rag.ingestor --all
```

### Launch

```powershell
.\.venv\Scripts\python.exe cli.py                 # auto-route
.\.venv\Scripts\python.exe cli.py --platform aws  # lock a platform
```

CLI commands: `/platform`, `/speak`, `/models`, `/save`, `/copy`, `/history`, `/search`,
`/replay`, `/reset`, `/quit`.

---

## Architecture

```
cli.py
  └─ orchestrator/pipeline.py ── router (NPU) ─┐
                                               ├─ agents/{databricks,snowflake,aws}_agent.py
                                               │     └─ core/llm_client.py ── Lemonade /v1 (iGPU coder)
                                               │     └─ tools/*  (mock-safe, OpenAI tool schemas)
                                               │     └─ rag/retriever.py (CPU embeddings + ChromaDB)
                                               └─ core/hybrid.py  (warms + tracks NPU + iGPU models)
  modalities/tts.py (/speak)   core/config.py (.env)   orchestrator/{session,history,exporter}.py
```

- **`core/llm_client.py`** — the single transport swap point (Lemonade OpenAI `/v1`).
- **`core/hybrid.py`** — NPU/iGPU model manager; warms both, powers `/models`.
- **`agents/base_agent.py`** — OpenAI tool-calling loop (max 8 iterations) + RAG injection.
- **`agents/planner.py`** — optional plan-then-act (`ENABLE_PLANNER=1`) on the NPU model.
- **`rag/`, `tools/`, `orchestrator/`** — reused from II verbatim (backend-agnostic).

## Claude-ecosystem bonus

Lemonade serves the **same loaded model** on `POST /v1/messages` (Anthropic-compatible).
Point Claude Code or any Anthropic-SDK tool at `http://localhost:13305` to use your local
model — no extra setup in III.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -v   # fully offline: no Lemonade, no network
```

Tools are mock-safe (missing credentials → realistic mock data), and the agent loop is
tested against a fake `LLMClient`, so the suite needs neither the server nor cloud creds.
