#!/usr/bin/env python3
"""
Phase-0 GATE A — tool-calling conformance for Lemonade's OpenAI-compatible /v1.

Stdlib only (no openai/requests dependency). Run after Lemonade is installed and
the coder model is loaded. MUST pass 3/3 before writing/trusting any agent code.

Tests, mirroring DSA_Agent_II's Ollama gate but on the OpenAI wire shape:
  1. single tool call         -> choices[0].message.tool_calls[0].function.{name,arguments}
  2. multi-turn tool result   -> assistant tool_call + role:"tool" (tool_call_id) completes
  3. no hallucinated tools    -> no tools provided => no tool_calls
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE  = os.getenv("LEMONADE_BASE_URL", "http://localhost:13305/v1").rstrip("/")
MODEL = os.getenv("AGENT_MODEL", "Qwen3-Coder-30B-A3B-Instruct-GGUF")
KEY   = os.getenv("LEMONADE_API_KEY", "lemonade")
OK    = "\033[92m[PASS]\033[0m"
FAIL  = "\033[91m[FAIL]\033[0m"

CLUSTER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_cluster_status",
        "description": "Get the current state of a Databricks cluster",
        "parameters": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "The Databricks cluster ID"}
            },
            "required": ["cluster_id"],
        },
    },
}


def post(messages, tools=None):
    payload = {"model": MODEL, "messages": messages, "stream": False, "max_tokens": 512}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read()), int((time.time() - t0) * 1000)


def _message(data):
    return (data.get("choices") or [{}])[0].get("message", {}) or {}


def test_single_tool_call():
    msgs = [{"role": "user", "content": "What is the status of cluster 0123-abc456?"}]
    try:
        data, ms = post(msgs, tools=[CLUSTER_TOOL])
        calls = _message(data).get("tool_calls") or []
        if calls and calls[0]["function"]["name"] == "get_cluster_status":
            args = calls[0]["function"]["arguments"]
            if "cluster_id" in (args if isinstance(args, str) else json.dumps(args)):
                print(f"{OK} Single tool call ({ms}ms) - args: {args}")
                return True
        print(f"{FAIL} No valid tool call returned. Message: {_message(data)}")
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} Request error: {e}")
    return False


def test_multi_turn():
    msgs = [
        {"role": "user", "content": "Check cluster 0123-abc456."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_cluster_status",
                             "arguments": json.dumps({"cluster_id": "0123-abc456"})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"state": "RUNNING", "num_workers": 4,
                                   "cluster_id": "0123-abc456", "driver": "Standard_DS3_v2"}),
        },
    ]
    try:
        data, ms = post(msgs)
        content = _message(data).get("content", "") or ""
        if "running" in content.lower():
            print(f"{OK} Multi-turn tool result injection ({ms}ms)")
            return True
        print(f"{FAIL} Expected 'RUNNING' in response. Got: {content[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} Request error: {e}")
    return False


def test_no_hallucination():
    msgs = [{"role": "user", "content": "What is 15 multiplied by 7?"}]
    try:
        data, ms = post(msgs)  # no tools registered
        if _message(data).get("tool_calls"):
            print(f"{FAIL} Phantom tool calls: {_message(data)['tool_calls']}")
            return False
        print(f"{OK} No hallucinated tool calls ({ms}ms)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} Request error: {e}")
        return False


if __name__ == "__main__":
    print("\n=== Lemonade OpenAI /v1 Tool-Call Gate Test ===")
    print(f"    Base:  {BASE}")
    print(f"    Model: {MODEL}\n")

    results = [test_single_tool_call(), test_multi_turn(), test_no_hallucination()]
    passed = sum(results)
    print(f"\n{'-' * 52}")
    print(f"  Result: {passed}/{len(results)} passed\n")

    if passed < len(results):
        print("Troubleshooting checklist:")
        print("  1. lemonade-server status            # service up on :13305?")
        print("  2. curl %LEMONADE_BASE_URL%/models   # model listed/loaded?")
        print("  3. lemonade-server load <model> --llamacpp rocm")
        print("  4. If tool_use is thin here, the plan's documented fallbacks are")
        print("     Lemonade's Ollama-compat (/api/chat) or Anthropic (/v1/messages);")
        print("     both are isolated to core/llm_client.py.")
        sys.exit(1)

    print("  All checks passed. Proceed to Phase 1.\n")
