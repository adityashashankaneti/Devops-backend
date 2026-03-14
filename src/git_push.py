"""
Push per-resource-type YAML configs to the devops-infra-live GitHub repository.

New approach:
  1. Create/update branch from main
  2. Write environments/<project>/project.yaml
  3. For each resource type:
     a. Write environments/<project>/<type>/terragrunt.hcl  (from template)
     b. Read existing resources.yaml → merge new resources → write back
  4. Open a Pull Request
"""

import logging
import yaml
from github import Github, GithubException

logger = logging.getLogger(__name__)


def _get_file_content(repo, path: str, ref: str) -> str | None:
    """Read a file from the repo, return content string or None."""
    try:
        contents = repo.get_contents(path, ref=ref)
        return contents.decoded_content.decode("utf-8")
    except GithubException:
        return None


def _get_file_sha(repo, path: str, ref: str) -> str | None:
    """Get the SHA of an existing file (needed for updates)."""
    try:
        contents = repo.get_contents(path, ref=ref)
        return contents.sha
    except GithubException:
        return None


def _upsert_file(repo, path: str, content: str, message: str, branch: str):
    """Create or update a file on the given branch."""
    sha = _get_file_sha(repo, path, branch)
    if sha:
        repo.update_file(path, message, content, sha, branch=branch)
        logger.info("Updated: %s", path)
    else:
        repo.create_file(path, message, content, branch=branch)
        logger.info("Created: %s", path)


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base.

    - Dicts are merged recursively.
    - Lists are concatenated and deduplicated (by converting each item to a
      canonical string key so that identical policy/rule entries aren't doubled).
    - Scalar values in override replace those in base.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result:
            if isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = _deep_merge(result[key], val)
            elif isinstance(result[key], list) and isinstance(val, list):
                # Concatenate; deduplicate by stable string representation
                seen = {str(item) for item in result[key]}
                extras = [item for item in val if str(item) not in seen]
                result[key] = result[key] + extras
            else:
                result[key] = val
        else:
            result[key] = val
    return result


def _merge_yaml_resources(existing_yaml: str | None, new_resources: dict) -> str:
    """
    Merge new resources into an existing YAML file.

    If the file already has a resource with the same key, the configs are
    deep-merged so that list fields (iam_policies, ingress_rules, etc.) are
    appended rather than overwritten.  New keys are added.
    """
    existing = yaml.safe_load(existing_yaml) or {} if existing_yaml else {}

    merged = dict(existing)
    for name, config in new_resources.items():
        if name in merged and isinstance(merged[name], dict) and isinstance(config, dict):
            merged[name] = _deep_merge(merged[name], config)
        else:
            merged[name] = config

    return yaml.dump(merged, default_flow_style=False, sort_keys=False)


def push_to_infra_repo(
    github_token: str,
    repo_name: str,
    branch_name: str,
    project: str,
    region: str,
    resource_map: dict[str, dict],
    terragrunt_hcls: dict[str, str],
    project_yaml: str,
    commit_message: str,
    base_branch: str = "main",
) -> dict:
    """
    Push per-resource-type YAML configs to the infra-live repo.

    Args:
        github_token:    GitHub PAT
        repo_name:       e.g. "your-org/devops-infra-live"
        branch_name:     e.g. "feature/my-infra"
        project:         Project name
        region:          AWS region
        resource_map:    { module_type: { resource_name: config } }
        terragrunt_hcls: { module_type: terragrunt.hcl content }
        project_yaml:    Content of project.yaml
        commit_message:  Git commit message
        base_branch:     Branch to create from

    Returns:
        dict with branch, commit_sha, pr_url, files_written
    """
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Get base branch SHA
    try:
        base_ref = repo.get_branch(base_branch)
        base_sha = base_ref.commit.sha
    except GithubException:
        base_ref = repo.get_branch("master")
        base_sha = base_ref.commit.sha
        base_branch = "master"

    # Create or reset the feature branch
    ref_path = f"refs/heads/{branch_name}"
    try:
        existing_ref = repo.get_git_ref(f"heads/{branch_name}")
        existing_ref.edit(sha=base_sha, force=True)
        logger.info("Reset branch %s to %s", branch_name, base_sha[:8])
    except GithubException:
        repo.create_git_ref(ref=ref_path, sha=base_sha)
        logger.info("Created branch %s from %s (%s)", branch_name, base_branch, base_sha[:8])

    env_prefix = "environments/dev"
    files_written = []

    # 1. Update project.yaml with project name + region
    proj_path = f"{env_prefix}/project.yaml"
    _upsert_file(repo, proj_path, project_yaml, commit_message, branch_name)
    files_written.append(proj_path)

    # 2. For each resource type: only merge resources.yaml
    #    (terragrunt.hcl already exists as a static file in the repo)
    for module_type, resources in resource_map.items():
        type_dir = f"{env_prefix}/{module_type}"

        # Merge resources.yaml (append new resources to existing)
        yaml_path = f"{type_dir}/resources.yaml"
        existing_yaml = _get_file_content(repo, yaml_path, branch_name)
        merged_yaml = _merge_yaml_resources(existing_yaml, resources)
        _upsert_file(repo, yaml_path, merged_yaml, commit_message, branch_name)
        files_written.append(yaml_path)

    # Get latest commit SHA
    branch_ref = repo.get_branch(branch_name)
    commit_sha = branch_ref.commit.sha

    # Create Pull Request
    pr_url = None
    try:
        module_types = sorted(resource_map.keys())
        resource_count = sum(len(v) for v in resource_map.values())

        pr = repo.create_pull(
            title=f"[DevOps AI] {commit_message}",
            body=(
                "## Auto-generated Infrastructure\n\n"
                "This PR was created by **DevOps AI** from the architecture canvas.\n\n"
                f"**Project:** `{project}` | **Region:** `{region}`\n"
                f"**Resources:** {resource_count} across {len(module_types)} module types\n\n"
                "### Module Types\n"
                + "\n".join(f"- `{mt}` ({len(resource_map[mt])} resources)" for mt in module_types)
                + "\n\n### Files\n"
                + "\n".join(f"- `{f}`" for f in files_written)
                + "\n\n### Next Steps\n"
                "1. Review the Terraform plan posted as a comment\n"
                "2. Merge to trigger `terragrunt run-all apply`\n"
            ),
            head=branch_name,
            base=base_branch,
        )
        pr_url = pr.html_url
        logger.info("Created PR: %s", pr_url)
    except GithubException as e:
        logger.warning("Could not create PR (may already exist): %s", e)
        # Try to find existing PR
        try:
            pulls = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
            for p in pulls:
                pr_url = p.html_url
                break
        except GithubException:
            pass

    return {
        "branch": branch_name,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "files_written": files_written,
    }


def get_pr_status(github_token: str, repo_name: str, pr_url: str) -> dict:
    """
    Check the CI/CD status of a Pull Request.

    Returns dict with: state, checks, conclusion
    """
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Extract PR number from URL
    pr_number = int(pr_url.rstrip("/").split("/")[-1])
    pr = repo.get_pull(pr_number)

    # Get combined commit status
    commit = repo.get_commit(pr.head.sha)
    statuses = commit.get_combined_status()

    # Get check runs (GitHub Actions)
    check_runs = commit.get_check_runs()
    checks = []
    for run in check_runs:
        checks.append({
            "name": run.name,
            "status": run.status,          # queued, in_progress, completed
            "conclusion": run.conclusion,  # success, failure, neutral, etc.
            "details_url": run.details_url,
        })

    overall = "pending"
    if checks:
        conclusions = [c["conclusion"] for c in checks if c["conclusion"]]
        if all(c == "success" for c in conclusions) and len(conclusions) == len(checks):
            overall = "success"
        elif any(c == "failure" for c in conclusions):
            overall = "failure"
        elif any(c["status"] == "in_progress" for c in checks):
            overall = "in_progress"

    return {
        "pr_number": pr_number,
        "pr_state": pr.state,
        "pr_merged": pr.merged,
        "overall_status": overall,
        "combined_status": statuses.state,
        "checks": checks,
    }
