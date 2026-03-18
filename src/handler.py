"""
DevOps AI Backend — Lambda Handler

Flow:
  1. Receive architecture JSON from frontend
  2. Send to Claude to classify resources into per-module-type YAML configs
  3. Push YAML files (merge/append) to devops-infra-live repo on feature branch
  4. Return branch URL + PR URL + commit SHA to frontend

Endpoints:
  POST /api/deploy   — Run the full deploy pipeline
  GET  /api/status    — Poll CI/CD status of a PR (query param: pr_url)
  GET  /api/health    — Health check
"""

import json
import os
import re
import logging
from generate_terraform import (
    generate_resource_yamls,
    build_terragrunt_hcl,
    build_project_yaml,
    resources_to_yaml,
    analyze_destroy,
)
from git_push import push_to_infra_repo, get_pr_status, push_destroy_to_main, get_commit_status, get_all_resources, get_module_terraform_code
from import_state import import_from_state
from secrets_helper import get_secret

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """AWS Lambda entry point."""

    path = event.get("rawPath", "") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    # Health check
    if path.endswith("/health") and method == "GET":
        return _response(200, {"status": "ok"})

    # Status polling
    if path.endswith("/status") and method == "GET":
        return _handle_status(event)

    # Chat — only POST
    if path.endswith("/chat") and method == "POST":
        return _handle_chat(event)

    # Deploy — only POST
    if path.endswith("/deploy") and method == "POST":
        return _handle_deploy(event)

    # Import from Terraform state — only GET
    if path.endswith("/import") and method == "GET":
        return _handle_import(event)

    # Destroy a resource — only POST
    if path.endswith("/destroy") and method == "POST":
        return _handle_destroy(event)

    # Poll commit status (for destroy apply tracking) — only GET
    if path.endswith("/commit-status") and method == "GET":
        return _handle_commit_status(event)

    return _response(405, {"error": "Method not allowed"})


def _handle_chat(event):
    """Handle POST /api/chat — send messages to Claude and return response."""

    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    messages = payload.get("messages", [])
    if not messages:
        return _response(400, {"error": "No messages provided"})

    # Validate message format
    for msg in messages:
        if msg.get("role") not in ("user", "assistant") or not msg.get("content"):
            return _response(400, {"error": "Invalid message format"})

    try:
        import anthropic

        anthropic_key = get_secret(
            os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", ""),
            fallback_env="ANTHROPIC_API_KEY",
        )

        client = anthropic.Anthropic(api_key=anthropic_key)

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            system=(
                "You are a DevOps and AWS cloud architecture expert assistant. "
                "Help users design cloud infrastructure, answer AWS service questions, "
                "explain DevOps practices, and give actionable architecture advice. "
                "Be concise and practical. Use markdown formatting where helpful."
            ),
            messages=messages,
        )

        reply = response.content[0].text

        logger.info("Chat response: input_tokens=%d output_tokens=%d",
                    response.usage.input_tokens, response.usage.output_tokens)

        return _response(200, {"reply": reply})

    except Exception as e:
        logger.exception("Chat failed")
        return _response(500, {"error": str(e)})


def _handle_deploy(event):
    """Handle POST /api/deploy — full pipeline."""

    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    if not payload.get("resources"):
        return _response(400, {"error": "No resources in payload"})

    project = re.sub(r'[^a-z0-9-]', '-', payload.get("project", "my-infra").lower())[:40].strip('-') or "my-infra"
    region = payload.get("region", "us-east-1")
    resources = payload.get("resources", [])
    connections = payload.get("connections", [])

    ALLOWED_MODELS = {"claude-opus-4-6"}
    model = payload.get("model", "claude-opus-4-6")
    if model not in ALLOWED_MODELS:
        model = "claude-opus-4-6"

    logger.info(
        "Deploy request: project=%s region=%s resources=%d connections=%d model=%s",
        project, region, len(resources), len(connections), model,
    )

    try:
        # Step 1: Call Claude to classify resources into per-module YAML
        anthropic_key = get_secret(
            os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", ""),
            fallback_env="ANTHROPIC_API_KEY",
        )
        resource_map = generate_resource_yamls(
            payload=payload,
            anthropic_api_key=anthropic_key,
            model=model,
        )

        # Step 2: Build project.yaml
        project_yaml = build_project_yaml(project, region)

        # Step 3: Push to infra-live repo (YAML merge/append into environments/dev/)
        github_token = get_secret(
            os.environ.get("GITHUB_TOKEN_SECRET_ARN", ""),
            fallback_env="GITHUB_TOKEN",
        )
        repo_name = os.environ.get("GITHUB_REPO", "your-org/devops-infra-live")
        branch_name = f"feature/{project}"

        modules_in_deploy = set(resource_map.keys())

        result = push_to_infra_repo(
            github_token=github_token,
            repo_name=repo_name,
            branch_name=branch_name,
            project=project,
            region=region,
            resource_map=resource_map,
            terragrunt_hcls={},
            project_yaml=project_yaml,
            commit_message=f"feat: deploy {project} ({region})",
        )

        return _response(200, {
            "status": "success",
            "branch": result["branch"],
            "commit_sha": result["commit_sha"],
            "pr_url": result.get("pr_url"),
            "files_written": result.get("files_written", []),
            "module_types": sorted(modules_in_deploy),
            "resource_count": sum(len(v) for v in resource_map.values()),
        })

    except Exception as e:
        logger.exception("Deploy failed")
        return _response(500, {"error": str(e)})


def _handle_status(event):
    """Handle GET /api/status — poll CI/CD status of a PR."""

    # Get pr_url from query params
    query = event.get("queryStringParameters") or {}
    pr_url = query.get("pr_url", "")

    if not pr_url:
        return _response(400, {"error": "Missing required query param: pr_url"})

    try:
        github_token = get_secret(
            os.environ.get("GITHUB_TOKEN_SECRET_ARN", ""),
            fallback_env="GITHUB_TOKEN",
        )
        repo_name = os.environ.get("GITHUB_REPO", "your-org/devops-infra-live")

        status = get_pr_status(
            github_token=github_token,
            repo_name=repo_name,
            pr_url=pr_url,
        )

        return _response(200, {"status": "ok", **status})

    except Exception as e:
        logger.exception("Status check failed")
        return _response(500, {"error": str(e)})


def _handle_destroy(event):
    """Handle POST /api/destroy — remove a resource from YAML and commit to main."""

    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    project       = payload.get("project", "").strip()
    region        = payload.get("region", "us-east-1").strip()
    resource_type = payload.get("resource_type", "").strip()
    resource_name = payload.get("resource_name", "").strip()
    env_dir       = payload.get("env_dir", "dev").strip() or "dev"
    # Frontend sends list of all deployed resources for dependency analysis
    frontend_deployed = payload.get("deployed_resources", [])

    if not all([project, resource_type, resource_name]):
        return _response(400, {"error": "Missing required fields: project, resource_type, resource_name"})

    logger.info("Destroy request: project=%s env=%s type=%s name=%s deployed_count=%d",
                project, env_dir, resource_type, resource_name, len(frontend_deployed))

    try:
        github_token = get_secret(
            os.environ.get("GITHUB_TOKEN_SECRET_ARN", ""),
            fallback_env="GITHUB_TOKEN",
        )
        repo_name = os.environ.get("GITHUB_REPO", "your-org/devops-infra-live")

        # Step 1: Read all deployed resources from the repo
        all_resources = get_all_resources(
            github_token=github_token,
            repo_name=repo_name,
            env_dir=env_dir,
        )

        # Step 1b: Read the actual Terraform module code for all deployed module types
        module_types = list(all_resources.keys())
        terraform_code = get_module_terraform_code(
            github_token=github_token,
            repo_name=repo_name,
            module_types=module_types,
        )

        # Step 2: Ask Claude to analyze dependencies and figure out
        # what needs to change to safely destroy this resource
        anthropic_key = get_secret(
            os.environ.get("ANTHROPIC_API_KEY_SECRET_ARN", ""),
            fallback_env="ANTHROPIC_API_KEY",
        )
        destroy_plan = analyze_destroy(
            resource_type=resource_type,
            resource_name=resource_name,
            all_resources=all_resources,
            anthropic_api_key=anthropic_key,
            frontend_deployed=frontend_deployed,
            terraform_code=terraform_code,
        )

        # If Claude says there's a blocking dependency, return 409 so frontend shows it
        if destroy_plan.get("blocked"):
            return _response(409, {
                "dependency_error": True,
                "explanation": destroy_plan.get("explanation", "Cannot destroy — dependencies exist."),
            })

        modules_to_update = destroy_plan.get("modules_to_update", {})
        destroy_order = destroy_plan.get("destroy_order", [resource_type])

        logger.info(
            "Destroy plan: %d modules affected: %s — %s",
            len(modules_to_update), list(modules_to_update.keys()),
            destroy_plan.get("explanation", ""),
        )

        # Step 3: Push all changes to main and trigger destroy workflow
        result = push_destroy_to_main(
            github_token=github_token,
            repo_name=repo_name,
            project=project,
            resource_type=resource_type,
            resource_name=resource_name,
            commit_message=f"destroy: remove {resource_type}/{resource_name} from {project}",
            modules_to_update=modules_to_update,
            destroy_order=destroy_order,
            env_dir=env_dir,
        )
        return _response(200, {
            "status": "ok",
            "explanation": destroy_plan.get("explanation", ""),
            **result,
        })

    except ValueError as e:
        return _response(404, {"error": str(e)})
    except Exception as e:
        logger.exception("Destroy failed")
        return _response(500, {"error": str(e)})


def _handle_commit_status(event):
    """Handle GET /api/commit-status?sha=... — poll check runs on a commit."""

    query = event.get("queryStringParameters") or {}
    commit_sha = query.get("sha", "").strip()

    if not commit_sha:
        return _response(400, {"error": "Missing required query param: sha"})

    try:
        github_token = get_secret(
            os.environ.get("GITHUB_TOKEN_SECRET_ARN", ""),
            fallback_env="GITHUB_TOKEN",
        )
        repo_name = os.environ.get("GITHUB_REPO", "your-org/devops-infra-live")

        status = get_commit_status(
            github_token=github_token,
            repo_name=repo_name,
            commit_sha=commit_sha,
        )
        return _response(200, status)

    except Exception as e:
        logger.exception("Commit status check failed")
        return _response(500, {"error": str(e)})


def _handle_import(event):
    """Handle GET /api/import — read Terraform state from S3 and return canvas nodes."""

    query = event.get("queryStringParameters") or {}
    project = query.get("project", "").strip()
    region  = query.get("region", "us-east-1").strip()

    if not project:
        return _response(400, {"error": "Missing required query param: project"})

    import re
    project = re.sub(r'[^a-z0-9-]', '-', project.lower())[:40].strip('-') or "my-infra"

    logger.info("Import request: project=%s region=%s", project, region)

    try:
        result = import_from_state(project=project, region=region)
        if "error" in result:
            return _response(404, result)
        return _response(200, result)
    except Exception as e:
        logger.exception("Import failed")
        return _response(500, {"error": str(e)})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body),
    }
