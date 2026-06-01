#!/usr/bin/env python3
"""Generate GitHub profile README from merged and open pull requests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "profile.config.yaml"
TEMPLATE_PATH = ROOT / "README.template.md"
OUTPUT_PATH = ROOT / "README.md"
CACHE_PATH = ROOT / ".cache" / "repos.json"

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"

SEARCH_QUERY = """
query($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        title
        url
        state
        isDraft
        mergedAt
        createdAt
        repository {
          nameWithOwner
          owner {
            login
          }
        }
      }
    }
  }
}
"""


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("GH_TOKEN or GITHUB_TOKEN environment variable is required")


def graphql_search_gh_cli(query: str) -> list[dict]:
    results: list[dict] = []
    cursor: str | None = None

    while True:
        variables: dict = {"query": query}
        if cursor:
            variables["cursor"] = cursor

        payload_input = json.dumps({"query": SEARCH_QUERY, "variables": variables})
        result = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=payload_input,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        payload = json.loads(result.stdout)
        if "errors" in payload:
            raise RuntimeError(json.dumps(payload["errors"], indent=2))

        search = payload["data"]["search"]
        for node in search["nodes"]:
            if node:
                results.append(node)

        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return results


def graphql_search_requests(token: str, query: str) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    session = requests.Session()
    session.trust_env = False

    results: list[dict] = []
    cursor: str | None = None

    while True:
        response = session.post(
            GRAPHQL_URL,
            headers=headers,
            json={"query": SEARCH_QUERY, "variables": {"query": query, "cursor": cursor}},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise RuntimeError(json.dumps(payload["errors"], indent=2))

        search = payload["data"]["search"]
        for node in search["nodes"]:
            if node:
                results.append(node)

        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return results


def graphql_search(token: str, query: str) -> list[dict]:
    if shutil_which("gh"):
        try:
            return graphql_search_gh_cli(query)
        except RuntimeError:
            pass
    return graphql_search_requests(token, query)


def shutil_which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def should_exclude_title(title: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, title) for pattern in patterns)


def fetch_pull_requests(token: str, username: str, config: dict) -> dict[str, dict]:
    merged_query = f"author:{username} is:pr is:merged"
    open_query = f"author:{username} is:pr is:open"

    exclude_owners = set(config.get("exclude_repo_owners", []))
    exclude_repos = set(config.get("exclude_repos", []))
    patterns = config.get("exclude_title_patterns", [])
    include_drafts = config.get("include_draft_prs", False)

    grouped: dict[str, dict] = defaultdict(
        lambda: {"merged": [], "open": [], "latest": None, "stars": 0}
    )

    merged_prs = graphql_search(token, merged_query)
    for pr in merged_prs:
        repo = pr["repository"]["nameWithOwner"]
        owner = pr["repository"]["owner"]["login"]
        title = pr["title"]

        if owner in exclude_owners or repo in exclude_repos:
            continue
        if should_exclude_title(title, patterns):
            continue

        merged_at = pr.get("mergedAt")
        grouped[repo]["merged"].append({"title": title, "at": merged_at})
        if merged_at and (
            grouped[repo]["latest"] is None or merged_at > grouped[repo]["latest"]
        ):
            grouped[repo]["latest"] = merged_at

    if config.get("include_open_prs", True):
        open_prs = graphql_search(token, open_query)
        for pr in open_prs:
            if pr.get("isDraft") and not include_drafts:
                continue
            if pr.get("state") != "OPEN":
                continue

            repo = pr["repository"]["nameWithOwner"]
            owner = pr["repository"]["owner"]["login"]
            title = pr["title"]

            if owner in exclude_owners or repo in exclude_repos:
                continue
            if should_exclude_title(title, patterns):
                continue

            created_at = pr.get("createdAt")
            grouped[repo]["open"].append({"title": title, "at": created_at})
            if created_at and (
                grouped[repo]["latest"] is None or created_at > grouped[repo]["latest"]
            ):
                grouped[repo]["latest"] = created_at

    return dict(grouped)


def load_cache() -> dict:
    if CACHE_PATH.exists():
        with CACHE_PATH.open(encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2)


def fetch_stars(token: str, repos: list[str], cache: dict) -> dict[str, int]:
    session = requests.Session()
    session.trust_env = False
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    stars: dict[str, int] = {}

    for repo in repos:
        if repo in cache:
            stars[repo] = cache[repo].get("stars", 0)
            continue

        response = session.get(f"{REST_URL}/repos/{repo}", headers=headers, timeout=30)
        if response.status_code == 404:
            stars[repo] = 0
            cache[repo] = {"stars": 0}
            continue
        response.raise_for_status()
        value = response.json().get("stargazers_count", 0)
        stars[repo] = value
        cache[repo] = {"stars": value}

    save_cache(cache)
    return stars


def sort_repos(grouped: dict[str, dict], stars: dict[str, int], config: dict) -> list[str]:
    sort_by = config.get("sort_by", "stars")
    reverse = config.get("sort_direction", "desc") == "desc"

    def pr_count(repo: str) -> int:
        return len(grouped[repo]["merged"]) + len(grouped[repo]["open"])

    if sort_by == "pr_count":
        key = lambda repo: pr_count(repo)
    elif sort_by == "recent":
        key = lambda repo: grouped[repo]["latest"] or ""
    else:
        key = lambda repo: stars.get(repo, 0)

    return sorted(grouped.keys(), key=key, reverse=reverse)


def limit_items(items: list[dict], limit: int) -> list[dict]:
    if limit <= 0:
        return items
    return items[:limit]


def render_contributions(grouped: dict[str, dict], stars: dict[str, int], config: dict) -> str:
    if not grouped:
        return "_No upstream contributions found yet — check back after your next merged PR!_"

    open_prefix = config.get("open_pr_prefix", "open:")
    max_merged = config.get("max_merged_prs_per_repo", 0)
    max_open = config.get("max_open_prs_per_repo", 0)

    lines: list[str] = []
    for repo in sort_repos(grouped, stars, config):
        merged = sorted(grouped[repo]["merged"], key=lambda item: item["at"] or "")
        open_prs = sorted(grouped[repo]["open"], key=lambda item: item["at"] or "")

        merged = limit_items(merged, max_merged)
        open_prs = limit_items(open_prs, max_open)

        titles = [item["title"] for item in merged]
        titles.extend(f"{open_prefix} {item['title']}" for item in open_prs)

        if not titles:
            continue

        title_text = ", ".join(titles)
        lines.append(
            f"- **[{repo}](https://github.com/{repo})** "
            f"[![GitHub stars](https://img.shields.io/github/stars/{repo}?style=flat&color=gold)]"
            f"(https://github.com/{repo}/stargazers) - {title_text}"
        )

    return "\n".join(lines) if lines else "_No upstream contributions found yet._"


def render_footer() -> str:
    return f"\n*Last updated: {date.today().isoformat()}*"


def build_readme(config: dict, contributions: str, footer: str) -> str:
    with TEMPLATE_PATH.open(encoding="utf-8") as handle:
        template = handle.read()

    if "<!-- AUTO:CONTRIBUTIONS -->" not in template:
        sys.exit("README.template.md is missing <!-- AUTO:CONTRIBUTIONS --> anchor")

    output = template.replace("<!-- AUTO:CONTRIBUTIONS -->", contributions)

    if "<!-- AUTO:FOOTER -->" in output:
        output = output.replace("<!-- AUTO:FOOTER -->", footer)
    else:
        output = output.rstrip() + "\n\n" + footer + "\n"

    return output


def main() -> None:
    config = load_config()
    token = get_token()
    username = config["github_username"]

    grouped = fetch_pull_requests(token, username, config)
    cache = load_cache()
    stars = fetch_stars(token, list(grouped.keys()), cache)

    contributions = render_contributions(grouped, stars, config)
    footer = render_footer()
    readme = build_readme(config, contributions, footer)

    OUTPUT_PATH.write_text(readme, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(grouped)} repos)")


if __name__ == "__main__":
    main()
