"""
RepoPulse – Lambda handler
Accepts a GitHub repo URL, fetches its file tree + README via the public
GitHub API, asks AWS Bedrock (Claude) to produce an architecture summary
and a Mermaid.js diagram, caches the result in DynamoDB, and returns JSON.
"""

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError


class _DecimalEncoder(json.JSONEncoder):
    """Convert DynamoDB Decimal values to native int or float for JSON serialisation."""
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)

# ── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients (initialised once per cold start) ──────────────────────────
dynamodb = boto3.resource("dynamodb")
bedrock  = boto3.client("bedrock-runtime")

TABLE_NAME       = os.environ["TABLE_NAME"]
BEDROCK_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"

# Cache TTL – 24 hours
CACHE_TTL_SECONDS = 24 * 60 * 60

# Max items in the file tree sent to the model (keeps the prompt manageable)
MAX_TREE_ITEMS = 200


# ── Helpers ────────────────────────────────────────────────────────────────

def _json_response(status_code: int, body: Any) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _parse_repo(repo_url: str) -> tuple[str, str]:
    url = repo_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    for prefix in ("https://", "http://", "git://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    parts = url.split("/")
    if len(parts) < 3 or parts[0].lower() != "github.com":
        raise ValueError(f"Cannot parse GitHub URL: {repo_url!r}")
    return parts[1], parts[2]


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_file_tree(owner: str, repo: str) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    resp = requests.get(url, headers=_github_headers(), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    paths = [
        item["path"]
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]
    return paths[:MAX_TREE_ITEMS]


def _fetch_readme(owner: str, repo: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    resp = requests.get(
        url,
        headers={**_github_headers(), "Accept": "application/vnd.github.raw"},
        timeout=10,
    )
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text[:4_000]


def _build_prompt(owner: str, repo: str, file_tree: list[str], readme: str) -> str:
    tree_text = "\n".join(file_tree) if file_tree else "(no files found)"
    readme_section = readme if readme else "(no README found)"
    return f"""You are an expert software engineer and technical writer. A developer has shared the following GitHub repository with you.

Repository: https://github.com/{owner}/{repo}

## File Tree
{tree_text}

## README
{readme_section}

Based on the above, please provide:
1. A concise architecture summary (2-4 paragraphs) describing what this repository does, its main components, and how they interact.
2. A Mermaid.js diagram (graph TD) that visually represents the architecture.

Format your response as JSON with two keys:
- "summary": the architecture summary as a string
- "diagram": the Mermaid.js diagram as a string (just the diagram code, no markdown fences)
"""
