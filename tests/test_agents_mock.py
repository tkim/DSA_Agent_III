"""
Lightweight agent unit tests that stub the LLM client so no model/server is
required. Verifies the OpenAI tool-dispatch loop wiring on each concrete agent.
"""
from __future__ import annotations

import os

import pytest

# Ensure mock mode for tools
for v in (
    "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "DATAHUB_GMS_URL", "DATAHUB_GMS_TOKEN",
):
    os.environ.pop(v, None)

from agents.aws_agent import AWSAgent
from agents.databricks_agent import DatabricksAgent
from agents.datahub_agent import DataHubAgent
from agents.snowflake_agent import SnowflakeAgent
from core.llm_client import LLMResponse, ToolCall


def _make_fake_client(tool_name: str, args: dict):
    """Client that first returns a tool_call, then a plain content reply."""
    calls = {"i": 0}

    class _FakeClient:
        def chat(self, **_kwargs):
            calls["i"] += 1
            if calls["i"] == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="call_1", name=tool_name, args=args)],
                )
            return LLMResponse(content="Done.", tool_calls=[])

    return _FakeClient()


@pytest.fixture(autouse=True)
def _stub_retrieve(monkeypatch):
    # retrieve() hits ChromaDB; return empty so the agent skips RAG.
    monkeypatch.setattr("agents.base_agent.retrieve", lambda platform, query: [])


def test_databricks_agent_runs_tool():
    agent = DatabricksAgent(model="test")
    agent.client = _make_fake_client("list_clusters", {})
    out = agent.run("list clusters")
    assert out["tool_calls_made"][0]["name"] == "list_clusters"
    assert out["response"] == "Done."


def test_snowflake_agent_runs_tool():
    agent = SnowflakeAgent(model="test")
    agent.client = _make_fake_client("list_warehouses", {})
    out = agent.run("warehouses")
    assert out["tool_calls_made"][0]["name"] == "list_warehouses"
    assert out["response"] == "Done."


def test_aws_agent_runs_tool():
    agent = AWSAgent(model="test")
    agent.client = _make_fake_client("list_s3_buckets", {})
    out = agent.run("list s3")
    assert out["tool_calls_made"][0]["name"] == "list_s3_buckets"
    assert out["response"] == "Done."


def test_datahub_agent_runs_tool():
    agent = DataHubAgent(model="test")
    agent.client = _make_fake_client("search_entities", {"query": "orders"})
    out = agent.run("find orders in datahub")
    assert out["tool_calls_made"][0]["name"] == "search_entities"
    assert out["tool_calls_made"][0]["result"]["_mock"] is True
    assert out["response"] == "Done."
