"""
RepoPulse – Lambda handler
Accepts a GitHub repo URL, fetches its file tree + README via the public
GitHub API, asks AWS Bedrock (Claude) to produce an architecture summary
and a Mermaid.js diagram, caches the result in DynamoDB, and returns JSON.
"""
import re
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any
from groq import Groq
import boto3
import requests
from botocore.exceptions import ClientError


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")


TABLE_NAME = os.environ["TABLE_NAME"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL_ID = "openai/gpt-oss-120b"

CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_TREE_ITEMS = 200


def _json_response(status_code: int, body: Any) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
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
    paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
    return paths[:MAX_TREE_ITEMS]


def _fetch_readme(owner: str, repo: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    resp = requests.get(url, headers={**_github_headers(), "Accept": "application/vnd.github.raw"}, timeout=10)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text[:4000]


def _build_prompt(owner: str, repo: str, file_tree: list[str], readme: str) -> str:
    tree_text = "\n".join(file_tree) if file_tree else "(no files found)"
    readme_section = readme if readme else "(no README found)"
    return (
        "You are an expert software engineer and technical writer. A developer has shared the following GitHub repository with you.\n\n"
        f"Repository: https://github.com/{owner}/{repo}\n\n"
        "## File Tree\n```\n" + tree_text + "\n```\n\n"
        "## README\n```\n" + readme_section + "\n```\n\n"
        "Based on the file tree and README above, produce two things:\n\n"
        "1. **Contributor Guide**: Write a structured onboarding guide using this exact Markdown format:\n\n"
        "**What it does**\n1-2 sentence plain-language description.\n\n"
        "**Key components**\n- Bullet list of main modules/folders and what each does (3-6 bullets)\n\n"
        "**How it works**\n- Bullet list of the key data/request flow steps\n\n"
        "**Tech stack**\n- Bullet list of main languages/frameworks/tools used\n\n"
        "**Getting started**\n- Bullet list of how to run it locally, if inferable\n\n"
        "**Where to look first**\n- Bullet list of the 1-3 most important files/folders\n\n"
        "2. **Mermaid.js Diagram**: Produce a single valid Mermaid.js diagram (flowchart TD). Output ONLY the Mermaid code block.\n\n"
        "Mermaid rules: use flowchart TD only; wrap every node label in double quotes, "
        'e.g. A["app.py (Streamlit)"]; no special characters outside quotes; '
        "no subgraph titles with spaces unless quoted.\n\n"
        "Format your response exactly like this:\n\n"
        "### Architecture Summary\n<your summary here>\n\n"
        "### Mermaid Diagram\n```mermaid\n<your diagram here>\n```"
    )


def _invoke_groq(prompt: str) -> str:
    client = Groq(api_key=GROQ_API_KEY)
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL_ID,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4000
              
            )
            return response.choices[0].message.content
        except Exception as exc:
            last_error = exc
            error_str = str(exc)
            if "429" in error_str or "rate_limit" in error_str.lower():
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
            raise
    raise last_error

def _cache_get(repo_url: str) -> dict | None:
    table = dynamodb.Table(TABLE_NAME)
    try:
        result = table.get_item(Key={"repo_url": repo_url})
        item = result.get("Item")
        if item and int(item.get("ttl", 0)) > int(time.time()):
            return item
    except ClientError as exc:
        logger.warning("DynamoDB get_item error: %s", exc)
    return None


def _cache_put(repo_url: str, summary: str, diagram: str) -> None:
    table = dynamodb.Table(TABLE_NAME)
    ttl = int(time.time()) + CACHE_TTL_SECONDS
    try:
        table.put_item(Item={
            "repo_url": repo_url,
            "summary": summary,
            "diagram": diagram,
            "ttl": ttl,
            "cached_at": int(time.time()),
        })
    except ClientError as exc:
        logger.warning("DynamoDB put_item error: %s", exc)


def _parse_model_output(raw_output: str):
    m = re.search(r"```mermaid\s*(.*?)```", raw_output, re.S)
    diagram = m.group(1).strip() if m else ""
    summary = re.sub(r"(###\s*Mermaid Diagram\s*)?```mermaid.*?```", "", raw_output, flags=re.S)
    summary = re.sub(r"^\s*###\s*Contributor Guide\s*", "", summary).strip()
    return summary, diagram

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("Event: %s", json.dumps(event))

    try:
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        elif isinstance(event.get("body"), dict):
            body = event["body"]
        else:
            body = {}
    except json.JSONDecodeError:
        return _json_response(400, {"error": "Invalid JSON in request body."})

    repo_url = (body.get("repo_url") or "").strip()
    if not repo_url:
        return _json_response(400, {"error": "Missing required field: repo_url"})

    cached = _cache_get(repo_url)
    if cached:
        logger.info("Cache hit for %s", repo_url)
        return _json_response(200, {
            "repo_url": repo_url,
            "summary": cached["summary"],
            "diagram": cached["diagram"],
            "cached": True,
            "cached_at": cached.get("cached_at"),
        })

    try:
        owner, repo = _parse_repo(repo_url)
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})

    try:
        file_tree = _fetch_file_tree(owner, repo)
        readme = _fetch_readme(owner, repo)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            return _json_response(404, {"error": f"Repository not found: {repo_url}"})
        logger.error("GitHub API error: %s", exc)
        return _json_response(502, {"error": "Error fetching data from GitHub API."})
    except requests.RequestException as exc:
        logger.error("GitHub request error: %s", exc)
        return _json_response(502, {"error": "Could not reach the GitHub API."})

    prompt = _build_prompt(owner, repo, file_tree, readme)
    try:
        raw_output = _invoke_groq(prompt)
    except Exception as exc:
        logger.exception("Groq error: %s", exc)
        return _json_response(502, {"error": f"Groq error: {exc}"})
    summary, diagram = _parse_model_output(raw_output)
    if diagram:
      _cache_put(repo_url, summary, diagram)

    return _json_response(200, {
        "repo_url": repo_url,
        "summary": summary,
        "diagram": diagram,
        "cached": False,
    })