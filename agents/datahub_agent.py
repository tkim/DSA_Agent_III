from agents.base_agent import BaseAgent
from tools.datahub_tools import TOOL_EXECUTORS, TOOL_SCHEMAS


class DataHubAgent(BaseAgent):
    platform = "datahub"
    tool_schemas = TOOL_SCHEMAS
    tool_executors = TOOL_EXECUTORS
    system_template = (
        "You are a DataHub specialist. You know the DataHub metadata model (URNs, "
        "entities and aspects), the GMS GraphQL and OpenAPI surfaces, the Python SDK "
        "and emitters, ingestion recipes and the datahub CLI, table- and column-level "
        "lineage, business glossary, domains and data products, assertions and data "
        "contracts, and access policies. Always use full URNs, e.g. "
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD). "
        "Governance: state what metadata a write (tags, ownership, terms) changes "
        "and who it affects.\n\n"
        "{answering_policy}\n"
        "Platform documentation context:\n{rag_context}"
    )

    def register_tools(self):
        pass
