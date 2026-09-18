"""
Pure-function tests for eval.evaluate._score_query. No server or LLM needed.
"""
from __future__ import annotations

from eval.evaluate import _score_query


def _result(*names: str, response: str = "", platform: str = "datahub") -> dict:
    return {
        "tool_calls_made": [{"name": n, "args": {}} for n in names],
        "response": response,
        "platform": platform,
    }


# --- single_tool ---------------------------------------------------------

SINGLE = {
    "id": "t_single",
    "type": "single_tool",
    "expected_tool": "list_clusters",
    "required_args": ["region"],
    "expected_arg_values": {"region": "us-east-1"},
}


def test_single_tool_match():
    res = {
        "tool_calls_made": [{"name": "list_clusters", "args": {"region": "us-east-1"}}],
        "platform": "databricks",
    }
    s = _score_query(SINGLE, res, "databricks")
    assert s["tool_ok"] and s["req_ok"] and s["val_ok"] and s["route_ok"]


def test_single_tool_wrong_first_tool():
    s = _score_query(SINGLE, _result("list_jobs", "list_clusters"), "datahub")
    assert not s["tool_ok"]


def test_single_tool_no_calls():
    s = _score_query(SINGLE, _result(), "datahub")
    assert not s["tool_ok"]


# --- multi_tool ----------------------------------------------------------

MULTI = {
    "id": "dh_011",
    "type": "multi_tool",
    "min_tool_calls": 2,
    "expected_tools_sequence": ["search_entities", "get_dataset"],
}


def test_multi_tool_exact_sequence():
    s = _score_query(MULTI, _result("search_entities", "get_dataset"), "datahub")
    assert s["tool_ok"]
    assert s["req_ok"] and s["val_ok"]


def test_multi_tool_subsequence_with_extra_calls():
    s = _score_query(
        MULTI, _result("search_entities", "get_lineage", "get_dataset"), "datahub"
    )
    assert s["tool_ok"]


def test_multi_tool_wrong_order():
    s = _score_query(MULTI, _result("get_dataset", "search_entities"), "datahub")
    assert not s["tool_ok"]


def test_multi_tool_missing_tool():
    s = _score_query(MULTI, _result("search_entities", "search_entities"), "datahub")
    assert not s["tool_ok"]


def test_multi_tool_too_few_calls():
    q = {**MULTI, "min_tool_calls": 3}
    s = _score_query(q, _result("search_entities", "get_dataset"), "datahub")
    assert not s["tool_ok"]


def test_multi_tool_no_calls():
    s = _score_query(MULTI, _result(), "datahub")
    assert not s["tool_ok"]


def test_multi_tool_single_min_call():
    q = {
        "id": "aws_014",
        "type": "multi_tool",
        "min_tool_calls": 1,
        "expected_tools_sequence": ["list_lambda_functions"],
    }
    assert _score_query(q, _result("list_lambda_functions"), "datahub")["tool_ok"]


# --- rag_only ------------------------------------------------------------

RAG = {
    "id": "t_rag",
    "type": "rag_only",
    "expected_tool": None,
    "expected_rag_keyword": "Lambda",
}


def test_rag_only_no_tools_keyword_present():
    s = _score_query(RAG, _result(response="Mitigate lambda cold starts..."), "datahub")
    assert s["tool_ok"] and s["rag_ok"]


def test_rag_only_keyword_missing():
    s = _score_query(RAG, _result(response="Nothing relevant."), "datahub")
    assert s["tool_ok"] and not s["rag_ok"]


def test_rag_only_tool_called_fails():
    s = _score_query(RAG, _result("list_lambda_functions", response="lambda"), "datahub")
    assert not s["tool_ok"]


def test_route_mismatch():
    s = _score_query(RAG, _result(response="lambda", platform="aws"), "snowflake")
    assert not s["route_ok"]
