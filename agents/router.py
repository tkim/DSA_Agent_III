"""
Router: keyword classifier with LLM fallback.
Returns one of: 'databricks', 'snowflake', 'aws', or 'ambiguous'.

The keyword layer is backend-free. The rare LLM fallback runs on the small
ROUTER_MODEL (ideally the NPU RyzenAI hybrid model) via the OpenAI-compatible
transport, keeping the iGPU free for the main coder model.
"""
from __future__ import annotations

from core import config
from core.llm_client import LLMClient

KEYWORDS = {
    "databricks": {
        "databricks", "unity catalog", "delta lake", "delta table", "mlflow",
        "mosaic ai", "dbfs", "dlt", "lakeflow", "autoloader", "dbsql",
        "databricks sql", "medallion", "lakehouse",
    },
    "snowflake": {
        "snowflake", "cortex", "snowpark", "iceberg", "snowpipe",
        "virtual warehouse", "time travel", "zero-copy clone", "tasks",
        "streams", "dynamic tables", "cortex analyst",
    },
    "aws": {
        "aws", "amazon", "s3", "glue", "bedrock", "lambda", "ec2", "ecs",
        "iam", "cloudformation", "cdk", "sagemaker", "athena", "redshift",
        "step functions", "sns", "sqs", "kinesis", "lakeformation", "boto3",
    },
}

# Strong identifiers score 2x — resolves ties where a generic term
# (e.g. "time travel") appears alongside a platform-specific product name.
STRONG_KEYWORDS = {
    "databricks": {
        "databricks", "unity catalog", "delta lake", "delta table", "mlflow",
        "dlt", "lakeflow", "dbsql", "databricks sql",
    },
    "snowflake": {"snowflake", "cortex", "snowpark", "snowpipe", "cortex analyst"},
    "aws": {
        "aws", "amazon", "bedrock", "boto3", "cloudformation",
        "sagemaker", "athena", "redshift", "lakeformation",
    },
}

# NOTE: the trailing "/no_think" disables Qwen3 "thinking" mode. Without it, a
# Qwen3 RyzenAI *Hybrid* router model (e.g. Qwen3-1.7B-Hybrid on the NPU) spends
# its small token budget emitting <think> reasoning and returns empty content,
# so the fallback would always yield "ambiguous". All models we use are Qwen3
# family, which recognizes this directive; it is harmless text otherwise.
LLM_PROMPT = (
    "Classify this cloud infrastructure query into exactly one of: "
    "databricks, snowflake, aws.\n"
    "Reply with ONLY the category name, lowercase, nothing else.\n\n"
    "Query: {query}\nCategory: /no_think"
)


class Router:
    def __init__(self, model: str | None = None):
        self.model = model or config.ROUTER_MODEL
        self.client = LLMClient()

    def route(self, query: str) -> str:
        """Returns 'databricks' | 'snowflake' | 'aws' | 'ambiguous'"""
        q = query.lower()
        scores = {
            p: sum(1 for kw in kws if kw in q)
               + sum(1 for kw in STRONG_KEYWORDS.get(p, set()) if kw in q)  # +1 bonus = 2x weight
            for p, kws in KEYWORDS.items()
        }
        top = max(scores, key=scores.get)

        if scores[top] > 0 and sum(v == scores[top] for v in scores.values()) == 1:
            return top

        # LLM fallback - tiny prompt, temperature=0
        try:
            resp = self.client.chat(
                model=self.model,
                messages=[{"role": "user", "content": LLM_PROMPT.format(query=query)}],
                temperature=0.0,
                max_tokens=24,  # room for a Qwen3 empty <think> wrapper + the answer
            )
            token = resp.content.strip().lower().split()[0] if resp.content else ""
        except Exception:
            return "ambiguous"
        return token if token in KEYWORDS else "ambiguous"
