"""
Unit tests for the OpenAI /v1 transport adapter's response normalization.
No server required — feeds the normalizer OpenAI-shaped fake responses.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.llm_client import LLMClient


def _openai_resp(content, tool_calls):
    tcs = []
    for i, (name, args) in enumerate(tool_calls):
        tcs.append(SimpleNamespace(
            id=f"call_{i}",
            function=SimpleNamespace(name=name, arguments=args),
        ))
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_normalize_plain_text():
    out = LLMClient._normalize(_openai_resp("hello", []))
    assert out.content == "hello"
    assert out.tool_calls == []


def test_normalize_tool_call_json_string_args():
    resp = _openai_resp("", [("get_cluster_status", '{"cluster_id": "abc"}')])
    out = LLMClient._normalize(resp)
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.name == "get_cluster_status"
    assert tc.args == {"cluster_id": "abc"}
    assert tc.id == "call_0"


def test_normalize_bad_json_args_degrade_to_empty():
    resp = _openai_resp("", [("f", "not-json")])
    out = LLMClient._normalize(resp)
    assert out.tool_calls[0].args == {}


def test_normalize_none_content():
    resp = _openai_resp(None, [])
    out = LLMClient._normalize(resp)
    assert out.content == ""
