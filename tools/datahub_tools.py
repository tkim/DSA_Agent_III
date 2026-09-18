"""
DataHub tool executors + OpenAI tool schemas.

Talks to DataHub GMS over its GraphQL API ({DATAHUB_GMS_URL}/api/graphql),
which is the same surface the DataHub UI uses and is identical for the OSS
quickstart and DataHub Cloud. Plain urllib is used rather than the
acryl-datahub SDK: the SDK is a large dependency tree (and pins pydantic /
avro versions) for what is, here, a single authenticated POST.

When DATAHUB_GMS_URL is not set, every tool returns a mock. DATAHUB_GMS_TOKEN
is optional — a local quickstart with metadata-service auth disabled needs
none; DataHub Cloud and auth-enabled deployments need a personal access token.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

from tools._common import env_ready, run_with_timeout, tool_wrapper

load_dotenv()

_LIVE_ENV = ("DATAHUB_GMS_URL",)
_HTTP_TIMEOUT_S = 12   # below _common's 15s so the HTTP error surfaces, not a bare timeout
_MAX_SCHEMA_FIELDS = 50

_MOCK_ORDERS_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.orders,PROD)"
_MOCK_CUSTOMERS_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.customers,PROD)"


def _graphql(query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL request to GMS and return `data`, raising on GraphQL errors."""
    url = f"{os.getenv('DATAHUB_GMS_URL', '').rstrip('/')}/api/graphql"
    headers = {"Content-Type": "application/json"}
    token = os.getenv("DATAHUB_GMS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"DataHub GMS HTTP {exc.code}: {detail}") from exc
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise RuntimeError(f"DataHub GraphQL error: {msgs}")
    return payload.get("data") or {}


def _entity_name(e: dict) -> str:
    """Best-effort display name across entity types."""
    props = e.get("properties") or {}
    return props.get("name") or props.get("displayName") or e.get("name") or e.get("urn", "")


# Shared selection for search / lineage hits: enough to identify each entity
# without dragging whole aspects back through the model's context window.
_ENTITY_SUMMARY = """
    urn
    type
    ... on Dataset { name platform { name } properties { name description } }
    ... on Dashboard { properties { name description } platform { name } }
    ... on Chart { properties { name description } platform { name } }
    ... on DataJob { properties { name description } }
    ... on DataFlow { properties { name description } platform { name } }
    ... on GlossaryTerm { properties { name definition } }
    ... on Domain { properties { name description } }
    ... on Container { properties { name description } platform { name } }
    ... on MLModel { name platform { name } }
"""


def _summarize(e: dict) -> dict:
    props = e.get("properties") or {}
    return {
        "urn": e.get("urn", ""),
        "type": e.get("type", ""),
        "name": _entity_name(e),
        "platform": (e.get("platform") or {}).get("name", ""),
        "description": props.get("description") or props.get("definition") or "",
    }


# --- search_entities ---------------------------------------------------------
@tool_wrapper("search_entities")
def search_entities(query: str, entity_type: str | None = None, count: int = 10) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "total": 2,
            "results": [
                {"urn": _MOCK_ORDERS_URN, "type": "DATASET", "name": "orders",
                 "platform": "snowflake", "description": "One row per customer order"},
                {"urn": _MOCK_CUSTOMERS_URN, "type": "DATASET", "name": "customers",
                 "platform": "snowflake", "description": "Customer master data"},
            ][:count],
        }

    def _live():
        gql = f"""
        query search($input: SearchAcrossEntitiesInput!) {{
          searchAcrossEntities(input: $input) {{
            total
            searchResults {{ entity {{ {_ENTITY_SUMMARY} }} }}
          }}
        }}"""
        inp: dict[str, Any] = {"query": query or "*", "start": 0, "count": int(count)}
        if entity_type:
            inp["types"] = [entity_type.upper()]
        res = _graphql(gql, {"input": inp})["searchAcrossEntities"]
        return {
            "total": res["total"],
            "results": [_summarize(r["entity"]) for r in res["searchResults"]],
        }

    return run_with_timeout(_live)


# --- get_dataset -------------------------------------------------------------
@tool_wrapper("get_dataset")
def get_dataset(urn: str) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "urn": urn,
            "name": "orders",
            "platform": "snowflake",
            "sub_types": ["Table"],
            "description": "One row per customer order",
            "fields": [
                {"path": "order_id",    "type": "NUMBER(38,0)",  "description": "Primary key"},
                {"path": "customer_id", "type": "NUMBER(38,0)",  "description": "FK to customers"},
                {"path": "order_ts",    "type": "TIMESTAMP_NTZ", "description": ""},
            ],
            "owners": [{"owner": "urn:li:corpuser:jdoe", "name": "jdoe"}],
            "tags": ["urn:li:tag:PII"],
            "glossary_terms": ["urn:li:glossaryTerm:Sales.Order"],
            "domain": {"urn": "urn:li:domain:sales", "name": "Sales"},
        }

    def _live():
        gql = """
        query getDataset($urn: String!) {
          dataset(urn: $urn) {
            urn
            name
            platform { name }
            subTypes { typeNames }
            properties { name description }
            schemaMetadata(version: 0) {
              fields { fieldPath nativeDataType description }
            }
            ownership {
              owners {
                owner {
                  ... on CorpUser { urn username }
                  ... on CorpGroup { urn name }
                }
              }
            }
            tags { tags { tag { urn } } }
            glossaryTerms { terms { term { urn } } }
            domain { domain { urn properties { name } } }
          }
        }"""
        d = _graphql(gql, {"urn": urn}).get("dataset")
        if not d:
            return {"error": f"Dataset not found: {urn}"}
        fields = ((d.get("schemaMetadata") or {}).get("fields")) or []
        owners = ((d.get("ownership") or {}).get("owners")) or []
        domain = (d.get("domain") or {}).get("domain") or {}
        return {
            "urn": d["urn"],
            "name": _entity_name(d),
            "platform": (d.get("platform") or {}).get("name", ""),
            "sub_types": (d.get("subTypes") or {}).get("typeNames") or [],
            "description": (d.get("properties") or {}).get("description") or "",
            "fields": [
                {"path": f["fieldPath"], "type": f.get("nativeDataType") or "",
                 "description": f.get("description") or ""}
                for f in fields[:_MAX_SCHEMA_FIELDS]
            ],
            "field_count": len(fields),
            "owners": [
                {"owner": o["owner"].get("urn", ""),
                 "name": o["owner"].get("username") or o["owner"].get("name", "")}
                for o in owners if o.get("owner")
            ],
            "tags": [t["tag"]["urn"] for t in ((d.get("tags") or {}).get("tags") or [])],
            "glossary_terms": [t["term"]["urn"] for t in ((d.get("glossaryTerms") or {}).get("terms") or [])],
            "domain": {"urn": domain.get("urn", ""),
                       "name": (domain.get("properties") or {}).get("name", "")} if domain else None,
        }

    return run_with_timeout(_live)


# --- get_lineage -------------------------------------------------------------
@tool_wrapper("get_lineage")
def get_lineage(urn: str, direction: str = "UPSTREAM", max_hops: int = 1, count: int = 50) -> dict:
    direction = (direction or "UPSTREAM").upper()
    if not env_ready(*_LIVE_ENV):
        other = _MOCK_CUSTOMERS_URN if direction == "UPSTREAM" else \
            "urn:li:dashboard:(looker,dashboards.revenue)"
        return {
            "_mock": True,
            "urn": urn,
            "direction": direction,
            "entities": [
                {"urn": other, "type": "DATASET" if direction == "UPSTREAM" else "DASHBOARD",
                 "name": "customers" if direction == "UPSTREAM" else "Revenue",
                 "platform": "snowflake" if direction == "UPSTREAM" else "looker",
                 "description": "", "degree": 1},
            ],
        }

    def _live():
        gql = f"""
        query lineage($input: SearchAcrossLineageInput!) {{
          searchAcrossLineage(input: $input) {{
            total
            searchResults {{ degree entity {{ {_ENTITY_SUMMARY} }} }}
          }}
        }}"""
        inp = {"urn": urn, "direction": direction, "query": "*", "start": 0, "count": int(count)}
        res = _graphql(gql, {"input": inp})["searchAcrossLineage"]
        # Filter hops client-side: the degree facet's value set ("1", "2", "3+")
        # is a UI convention, whereas `degree` on each result is part of the schema.
        hits = [r for r in res["searchResults"] if int(r.get("degree") or 0) <= int(max_hops)]
        return {
            "urn": urn,
            "direction": direction,
            "total_all_hops": res["total"],
            "entities": [_summarize(r["entity"]) | {"degree": r["degree"]} for r in hits],
        }

    return run_with_timeout(_live)


# --- list_platforms ----------------------------------------------------------
@tool_wrapper("list_platforms")
def list_platforms() -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "platforms": [
                {"urn": "urn:li:dataPlatform:snowflake",  "name": "Snowflake",  "entity_count": 1843},
                {"urn": "urn:li:dataPlatform:databricks", "name": "Databricks", "entity_count": 962},
                {"urn": "urn:li:dataPlatform:looker",     "name": "Looker",     "entity_count": 311},
            ],
        }

    def _live():
        gql = """
        query platforms($input: AggregateAcrossEntitiesInput!) {
          aggregateAcrossEntities(input: $input) {
            facets {
              field
              aggregations {
                value
                count
                entity { ... on DataPlatform { name properties { displayName } } }
              }
            }
          }
        }"""
        res = _graphql(gql, {"input": {"query": "*", "facets": ["platform"]}})
        facets = (res.get("aggregateAcrossEntities") or {}).get("facets") or []
        out = []
        for f in facets:
            if f.get("field") != "platform":
                continue
            for a in f.get("aggregations") or []:
                ent = a.get("entity") or {}
                out.append({"urn": a["value"], "name": _entity_name(ent) or a["value"],
                            "entity_count": int(a.get("count") or 0)})
        return out

    return {"platforms": run_with_timeout(_live)}


# --- list_domains ------------------------------------------------------------
@tool_wrapper("list_domains")
def list_domains(count: int = 20) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "domains": [
                {"urn": "urn:li:domain:sales",   "name": "Sales",   "description": "Orders, pipeline, revenue"},
                {"urn": "urn:li:domain:finance", "name": "Finance", "description": "GL, billing, forecasting"},
            ][:count],
        }

    def _live():
        gql = """
        query domains($input: ListDomainsInput!) {
          listDomains(input: $input) {
            total
            domains { urn properties { name description } }
          }
        }"""
        res = _graphql(gql, {"input": {"start": 0, "count": int(count)}})["listDomains"]
        return [
            {"urn": d["urn"], "name": (d.get("properties") or {}).get("name", ""),
             "description": (d.get("properties") or {}).get("description") or ""}
            for d in res["domains"]
        ]

    return {"domains": run_with_timeout(_live)}


# --- list_glossary_terms -----------------------------------------------------
@tool_wrapper("list_glossary_terms")
def list_glossary_terms(query: str = "*", count: int = 20) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "terms": [
                {"urn": "urn:li:glossaryTerm:Sales.Order", "name": "Order",
                 "definition": "A confirmed customer purchase"},
                {"urn": "urn:li:glossaryTerm:Classification.PII", "name": "PII",
                 "definition": "Personally identifiable information"},
            ][:count],
        }

    def _live():
        # searchAcrossEntities rather than getRootGlossaryTerms: the latter only
        # returns terms with no parent node, which misses most real glossaries.
        gql = """
        query terms($input: SearchAcrossEntitiesInput!) {
          searchAcrossEntities(input: $input) {
            searchResults { entity { urn ... on GlossaryTerm { properties { name definition } } } }
          }
        }"""
        inp = {"types": ["GLOSSARY_TERM"], "query": query or "*", "start": 0, "count": int(count)}
        res = _graphql(gql, {"input": inp})["searchAcrossEntities"]
        return [
            {"urn": r["entity"]["urn"],
             "name": (r["entity"].get("properties") or {}).get("name", ""),
             "definition": (r["entity"].get("properties") or {}).get("definition", "")}
            for r in res["searchResults"]
        ]

    return {"terms": run_with_timeout(_live)}


# --- get_dataset_assertions --------------------------------------------------
@tool_wrapper("get_dataset_assertions")
def get_dataset_assertions(urn: str, count: int = 10) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "urn": urn,
            "assertions": [
                {"urn": "urn:li:assertion:freshness-orders", "type": "FRESHNESS",
                 "description": "orders updated in the last 6 hours", "last_result": "SUCCESS"},
                {"urn": "urn:li:assertion:volume-orders", "type": "VOLUME",
                 "description": "row count > 0", "last_result": "FAILURE"},
            ][:count],
        }

    def _live():
        gql = """
        query assertions($urn: String!, $count: Int) {
          dataset(urn: $urn) {
            assertions(start: 0, count: $count) {
              total
              assertions {
                urn
                info { type description }
                runEvents(status: COMPLETE, limit: 1) { runEvents { timestampMillis result { type } } }
              }
            }
          }
        }"""
        d = _graphql(gql, {"urn": urn, "count": int(count)}).get("dataset")
        if not d:
            return {"error": f"Dataset not found: {urn}"}
        out = []
        for a in ((d.get("assertions") or {}).get("assertions")) or []:
            events = ((a.get("runEvents") or {}).get("runEvents")) or []
            last = ((events[0].get("result") or {}).get("type")) if events else None
            out.append({"urn": a["urn"],
                        "type": (a.get("info") or {}).get("type", ""),
                        "description": (a.get("info") or {}).get("description") or "",
                        "last_result": last or "NO_RUNS"})
        return {"urn": urn, "assertions": out}

    return run_with_timeout(_live)


# --- list_ingestion_sources --------------------------------------------------
@tool_wrapper("list_ingestion_sources")
def list_ingestion_sources(count: int = 20) -> dict:
    if not env_ready(*_LIVE_ENV):
        return {
            "_mock": True,
            "sources": [
                {"urn": "urn:li:dataHubIngestionSource:snowflake-prod", "name": "Snowflake prod",
                 "type": "snowflake", "schedule": "0 2 * * *", "timezone": "UTC"},
                {"urn": "urn:li:dataHubIngestionSource:dbt-cloud", "name": "dbt Cloud",
                 "type": "dbt-cloud", "schedule": None, "timezone": None},
            ][:count],
        }

    def _live():
        gql = """
        query sources($input: ListIngestionSourcesInput!) {
          listIngestionSources(input: $input) {
            total
            ingestionSources { urn name type schedule { interval timezone } }
          }
        }"""
        res = _graphql(gql, {"input": {"start": 0, "count": int(count)}})["listIngestionSources"]
        return [
            {"urn": s["urn"], "name": s["name"], "type": s["type"],
             "schedule": (s.get("schedule") or {}).get("interval"),
             "timezone": (s.get("schedule") or {}).get("timezone")}
            for s in res["ingestionSources"]
        ]

    return {"sources": run_with_timeout(_live)}


# --- add_tag -----------------------------------------------------------------
@tool_wrapper("add_tag")
def add_tag(resource_urn: str, tag_urn: str) -> dict:
    if not tag_urn.startswith("urn:li:tag:"):
        tag_urn = f"urn:li:tag:{tag_urn}"
    if not env_ready(*_LIVE_ENV):
        return {"_mock": True, "resource_urn": resource_urn, "tag_urn": tag_urn, "added": True}

    def _live():
        gql = """
        mutation addTag($input: TagAssociationInput!) {
          addTag(input: $input)
        }"""
        res = _graphql(gql, {"input": {"tagUrn": tag_urn, "resourceUrn": resource_urn}})
        return {"resource_urn": resource_urn, "tag_urn": tag_urn, "added": bool(res.get("addTag"))}

    return run_with_timeout(_live)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": "Search the DataHub catalog (datasets, dashboards, jobs, glossary terms, ...) by keyword",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search text; '*' matches everything"},
                    "entity_type": {
                        "type": "string",
                        "description": "Optional DataHub EntityType filter, e.g. DATASET, DASHBOARD, CHART, DATA_JOB, GLOSSARY_TERM",
                    },
                    "count": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dataset",
            "description": "Get a DataHub dataset's schema, owners, tags, glossary terms and domain by URN",
            "parameters": {
                "type": "object",
                "properties": {
                    "urn": {"type": "string", "description": "Dataset URN, e.g. urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD)"},
                },
                "required": ["urn"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lineage",
            "description": "Get upstream or downstream lineage for a DataHub entity URN",
            "parameters": {
                "type": "object",
                "properties": {
                    "urn": {"type": "string"},
                    "direction": {"type": "string", "enum": ["UPSTREAM", "DOWNSTREAM"], "default": "UPSTREAM"},
                    "max_hops": {"type": "integer", "default": 1},
                },
                "required": ["urn"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_platforms",
            "description": "List data platforms ingested into DataHub with entity counts",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_domains",
            "description": "List DataHub domains",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 20}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_glossary_terms",
            "description": "List or search DataHub business glossary terms",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": "*"},
                    "count": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dataset_assertions",
            "description": "Get data quality assertions on a DataHub dataset and their latest run result",
            "parameters": {
                "type": "object",
                "properties": {
                    "urn": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                },
                "required": ["urn"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ingestion_sources",
            "description": "List UI-managed DataHub ingestion sources and their schedules",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 20}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_tag",
            "description": "Add a tag to a DataHub entity (writes metadata)",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_urn": {"type": "string"},
                    "tag_urn": {"type": "string", "description": "Tag URN (urn:li:tag:PII) or bare tag name (PII)"},
                },
                "required": ["resource_urn", "tag_urn"],
            },
        },
    },
]


TOOL_EXECUTORS = {
    "search_entities": search_entities,
    "get_dataset": get_dataset,
    "get_lineage": get_lineage,
    "list_platforms": list_platforms,
    "list_domains": list_domains,
    "list_glossary_terms": list_glossary_terms,
    "get_dataset_assertions": get_dataset_assertions,
    "list_ingestion_sources": list_ingestion_sources,
    "add_tag": add_tag,
}
