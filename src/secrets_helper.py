"""
Retrieve secrets from AWS Secrets Manager, with env-var fallback for local dev.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}


def get_secret(secret_arn: str, fallback_env: str = "") -> str:
    """
    Get a secret value. Priority:
      1. Cached value
      2. AWS Secrets Manager (if ARN provided)
      3. Environment variable fallback (for local dev)
    """
    if secret_arn and secret_arn in _cache:
        return _cache[secret_arn]

    if secret_arn:
        try:
            import boto3
            client = boto3.client("secretsmanager")
            resp = client.get_secret_value(SecretId=secret_arn)
            value = resp["SecretString"]
            # Handle JSON-wrapped secrets
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    value = next(iter(parsed.values()))
            except (json.JSONDecodeError, StopIteration):
                pass
            _cache[secret_arn] = value
            return value
        except Exception as e:
            logger.warning("Failed to fetch secret from Secrets Manager: %s", e)

    # Fallback to environment variable
    if fallback_env:
        value = os.environ.get(fallback_env, "")
        if value:
            return value

    raise ValueError(
        f"Secret not found. ARN='{secret_arn}', env='{fallback_env}'"
    )
