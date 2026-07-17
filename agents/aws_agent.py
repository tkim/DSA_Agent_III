from agents.base_agent import BaseAgent
from tools.aws_tools import TOOL_EXECUTORS, TOOL_SCHEMAS


class AWSAgent(BaseAgent):
    platform = "aws"
    tool_schemas = TOOL_SCHEMAS
    tool_executors = TOOL_EXECUTORS
    system_template = (
        "You are an AWS specialist. You know IAM least-privilege policies, S3 lifecycle "
        "rules and intelligent tiering, Glue Data Catalog crawler patterns, Bedrock "
        "model invocation and agents, Lambda cold-start mitigation, and CDK L1/L2/L3 "
        "constructs. Rule: explain security implications of every IAM change.\n\n"
        "{answering_policy}\n"
        "Platform documentation context:\n{rag_context}"
    )

    def register_tools(self):
        pass
