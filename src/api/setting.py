import os

API_ROUTE_PREFIX = os.environ.get("API_ROUTE_PREFIX", "/api/v1")

TITLE = "Amazon Bedrock Proxy APIs"
SUMMARY = "OpenAI-Compatible RESTful APIs for Amazon Bedrock"
VERSION = "0.1.0"
DESCRIPTION = """
Use OpenAI-Compatible RESTful APIs for Amazon Bedrock models.
"""

DEBUG = os.environ.get("DEBUG", "false").lower() != "false"
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "anthropic.claude-3-sonnet-20240229-v1:0")
DEFAULT_EMBEDDING_MODEL = os.environ.get("DEFAULT_EMBEDDING_MODEL", "cohere.embed-multilingual-v3")
ENABLE_CROSS_REGION_INFERENCE = os.environ.get("ENABLE_CROSS_REGION_INFERENCE", "true").lower() != "false"
ENABLE_APPLICATION_INFERENCE_PROFILES = os.environ.get("ENABLE_APPLICATION_INFERENCE_PROFILES", "true").lower() != "false"
ENABLE_PROMPT_CACHING = os.environ.get("ENABLE_PROMPT_CACHING", "false").lower() != "false"

# Mantle (bedrock-mantle) is Bedrock's native OpenAI-compatible endpoint. When
# enabled, models served by Mantle are proxied through as-is (no Converse
# translation); everything else falls through to bedrock-runtime/Converse.
ENABLE_MANTLE = os.environ.get("ENABLE_MANTLE", "false").lower() != "false"
# Region for the bedrock-mantle endpoint; defaults to AWS_REGION when unset.
MANTLE_REGION = os.environ.get("MANTLE_REGION", AWS_REGION)
