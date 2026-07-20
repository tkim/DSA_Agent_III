# DSA Agent III

An AMD-native, fully-local infrastructure agent for **Databricks**, **Snowflake**, and
**AWS** — built on **AMD Lemonade Server 11.0**, running the 30B coder on the **iGPU** with
a small **NPU** hybrid model for routing, plus **spoken answers** via local TTS. Tuned for
the Asus Z13 (Ryzen AI Max+ 395 / Strix Halo, 128 GB unified, 96 GB VGM).

Successor to [DSA_Agent_II](../DSA_Agent_II). **No Ollama** — Lemonade is the only runtime.
III shares nothing with II except the GPU, so the two run side by side.

---

## Quick start (already set up)

```powershell
cd C:\Users\<you>\Documents\GitHub\DSA_Agent_III
.\infra\start_server.ps1                      # ensure Lemonade is up on :13305 (no-op if already)
.\.venv\Scripts\python.exe cli.py             # launch the agent
```

Then just type a question. Type `/speak` to hear the last answer, `/models` to see the
iGPU+NPU placement, `/quit` to exit.

If this is a fresh machine, do **[First-time setup](#first-time-setup)** first.

---

## Using the agent

Launch:

```powershell
.\.venv\Scripts\python.exe cli.py                  # auto-route each question
.\.venv\Scripts\python.exe cli.py --platform aws   # lock to one platform
.\.venv\Scripts\python.exe cli.py --no-history     # don't persist Q&A
```

At startup it warms the iGPU coder + NPU router, connects the RAG store and history, and
shows a `you>` prompt. Just type a question in plain English — it auto-routes to Databricks,
Snowflake, or AWS, retrieves relevant docs, calls tools as needed, and answers.

### Example session

```
you> When should I use Liquid Clustering vs partitioning in Delta Lake?
  ...answer grounded in docs.databricks.com, with a Source: line...
  platform=databricks  4317ms  rag=[delta-clustering.txt, ...]

you> list my S3 buckets and their object counts
  ...calls list_s3_buckets + get_s3_object_count...
  platform=aws  2100ms  tools=[list_s3_buckets, get_s3_object_count]

you> /speak            # hear that last answer read aloud
you> /save aws-buckets # write the answer to ~/Downloads as markdown
you> /quit
```

### Commands

| Command | What it does |
|---|---|
| *(any question)* | Auto-routed to Databricks / Snowflake / AWS, answered with tools + RAG |
| `/platform <name>` | Lock to `databricks` / `snowflake` / `aws` (or `auto` to unlock) |
| **`/speak`** | Read the last answer aloud via local TTS (see [Voice](#voice--tts)) |
| `/models` | Show loaded models and their **device** (iGPU coder / NPU router) |
| `/save [name]` | Export the last answer to `~/Downloads` as markdown |
| `/copy` | Copy the last answer (raw markdown) to the clipboard |
| `/history [N]` | List the last N persisted turns |
| `/search <kw>` | Full-text search across all past Q&A |
| `/replay <id>` | Re-render a stored turn by id |
| `/reset` | Clear the in-session conversation history |
| `/quit` | Exit (Ctrl-C also works) |

### Mock mode vs live

Cloud credentials are **blank in `.env` by default**, so every tool returns realistic
**mock data** — the agent is fully usable offline with no accounts. To hit live
infrastructure, fill in the `DATABRICKS_* / SNOWFLAKE_* / AWS_*` values in `.env`; each tool
switches to live automatically once its credentials are present.

---

## Voice / TTS

`/speak` synthesizes the last answer to a WAV via Lemonade's local TTS and plays it. It's
enabled in `.env` (`ENABLE_TTS=1`, `TTS_MODEL=kokoro-v1`). TTS uses a separate model slot,
so it never evicts the LLMs.

### Swapping the voice model

Three built-in options (all local, from Hugging Face via Lemonade):

| Model | Size | Notes |
|---|---|---|
| **kokoro-v1** (default) | 0.35 GB | Small, fast, natural — best for reading answers aloud |
| **MOSS-VoiceGen** | 7.3 GB | Voice cloning / voice-design |
| **OpenMOSS-TTS** | 12.5 GB | Full OpenMOSS TTS |

Swap in one command (pulls the model if needed, updates `.env`):

```powershell
.\infra\set_tts.ps1 MOSS-VoiceGen           # switch to MOSS-VoiceGen
.\infra\set_tts.ps1 kokoro-v1 -Voice af_bella   # back to kokoro with a named voice
```

Restart `cli.py` after swapping; the first `/speak` loads the new voice model. To do it by
hand instead, set `TTS_MODEL` (and optionally `TTS_VOICE`) in `.env`. `lemonade list` shows
every TTS-capable model (recipe `kokoro` or `openmoss`).

---

## First-time setup

> All steps need internet. After setup the agent runs fully offline.

### 1. Install Lemonade 11.0

```powershell
.\infra\01_install_lemonade.ps1     # downloads + runs the official lemonade.msi, verifies :13305
```

The installer registers a tray autostart that runs the server as `LemonadeServer.exe
--silent`. **Don't start a second copy by hand** — two instances fight over :13305. If it's
ever down, `.\infra\start_server.ps1` brings up exactly one.

### 2. Configure Lemonade + pull models

```powershell
.\infra\02_pull_models.ps1
```

This sets `max_loaded_models=2` (so the iGPU coder and NPU router stay resident together)
and `llamacpp.backend=auto`, then pulls the 30B coder, an NPU hybrid model, and `kokoro-v1`.
Note the hybrid model name it prints.

> **Backend note:** on this gfx1151 build, forcing `llamacpp.backend=rocm` makes the 30B
> load fail (HTTP 500); `auto` loads and runs at ~36 tok/s. The script uses `auto`.

### 3. Python environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -e .
copy .env.example .env
.\.venv\Scripts\python.exe -m pytest tests\ -q        # offline mock suite (expect 44 passed)
```

Then edit `.env`: set `ROUTER_MODEL` / `PLANNER_MODEL` to the hybrid model from step 2
(e.g. `Qwen3-1.7B-Hybrid`). `TTS_MODEL=kokoro-v1` and `ENABLE_TTS=1` are already set.

### 4. Prove tool-calling (Gate A)

```powershell
.\.venv\Scripts\python.exe .\infra\03_verify_toolcall.py    # must pass 3/3
.\infra\04_benchmark.ps1                                     # record iGPU tok/s
```

### 5. Build the RAG corpus (one-time)

```powershell
.\rag\fetch_docs.ps1
```

Scrapes official platform docs (docs.databricks.com + docs.snowflake.com via sitemap) and
AWS SDK references, then embeds them into a local ChromaDB. Takes a while (tens of thousands
of pages); it stages writes and swaps atomically, so it's safe to interrupt.

You're done — launch with [Quick start](#quick-start-already-set-up).

---

## Why III (vs II)

| | DSA_Agent_II | **DSA_Agent_III** |
|---|---|---|
| Runtime | Ollama (:11434) | **Lemonade 11.0 (:13305)** |
| Transport | ollama client | **OpenAI `/v1`** (`openai` SDK) |
| Silicon | iGPU only | **iGPU coder + NPU hybrid router** |
| Modality | text | text + **TTS** (`/speak`), optional image/3D |
| Models from | Ollama registry | **Hugging Face** (Lemonade default) |
| Bonus | — | same model also on **Anthropic `/v1/messages`** for Claude Code |

Raw tok/s is roughly par with II. The wins are the NPU hybrid, spoken answers, comprehensive
platform-doc RAG, and standard multi-API access — not speed.

## Coexistence with DSA_Agent_II — nothing breaks

Because III uses no Ollama, the only shared resource is the iGPU/VRAM.

| Resource | II | III |
|---|---|---|
| Server / port | Ollama :11434 | Lemonade :13305 |
| Client dep | `ollama` | `openai` |
| History DB | `~/.dsa_agent/` | `~/.dsa_agent_iii/` |
| RAG corpus | `II/rag/chroma_db` | `III/rag/chroma_db` |

> Keep Lemonade on **:13305** (it *can* be told to bind Ollama's 11434 — don't). Under heavy
> use, don't keep both 30B models hot at once; they share the 96 GB VGM.

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
- **`agents/router.py`** — keyword scorer; the rare LLM fallback runs on the NPU model.
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

Tools are mock-safe (missing credentials → realistic mock data) and the agent loop is tested
against a fake `LLMClient`, so the suite (44 tests) needs neither the server nor cloud creds.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `connection refused` on :13305 | Server down or two instances collided. Run `.\infra\start_server.ps1`. Never launch `LemonadeServer.exe` by hand while the tray one runs. |
| `/models` shows only the coder, router keeps reloading | `max_loaded_models` < 2. Run `lemonade config set max_loaded_models=2`. |
| 30B load fails with HTTP 500 | `llamacpp.backend` forced to `rocm`. Run `lemonade config set llamacpp.backend=auto`. |
| `/speak` fails | Pull a TTS model: `.\infra\set_tts.ps1 kokoro-v1`. |
| Router always returns "ambiguous" | NPU hybrid model is a Qwen3 "thinking" model — the router already appends `/no_think`; confirm `ROUTER_MODEL` is set to a pulled hybrid. |
