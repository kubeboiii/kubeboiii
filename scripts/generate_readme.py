#!/usr/bin/env python3
"""Generate GitHub profile README from merged and open pull requests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "profile.config.yaml"
CNCF_PATH = ROOT / "config" / "cncf_landscape.yaml"
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
        lambda: {"merged": [], "open": [], "latest": None}
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
        grouped[repo]["merged"].append(
            {"title": title, "url": pr["url"], "at": merged_at}
        )
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
            grouped[repo]["open"].append(
                {"title": title, "url": pr["url"], "at": created_at}
            )
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


def count_prs(grouped: dict[str, dict]) -> tuple[int, int]:
    merged = sum(len(data["merged"]) for data in grouped.values())
    open_count = sum(len(data["open"]) for data in grouped.values())
    return merged, open_count


def ecosystem_tag(repo: str, config: dict) -> str | None:
    explicit = config.get("ecosystem_tags", {}) or {}
    if repo in explicit:
        return explicit[repo]

    lower_repo = repo.lower()
    for prefix, tag in (config.get("ecosystem_prefixes", {}) or {}).items():
        if lower_repo.startswith(prefix.lower()):
            return tag

    return None


def sort_repos(grouped: dict[str, dict], stars: dict[str, int], config: dict) -> list[str]:
    flagship = config.get("flagship_repos", []) or []
    flagship_set = set(flagship)
    present_flagship = [repo for repo in flagship if repo in grouped]

    remaining = [repo for repo in grouped if repo not in flagship_set]
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

    remaining_sorted = sorted(remaining, key=key, reverse=reverse)
    return present_flagship + remaining_sorted


def limit_items(items: list[dict], limit: int) -> list[dict]:
    if limit <= 0:
        return items
    return items[:limit]


def format_star_count(count: int) -> str:
    if count >= 1_000_000:
        text = f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        text = f"{count / 1_000:.1f}k"
    else:
        return str(count)
    return text.replace(".0M", "M").replace(".0k", "k")


def star_badge_url(count: int) -> str:
    label = format_star_count(count)
    return f"https://img.shields.io/badge/stars-{label}-gold?style=flat"


def static_badge(label: str, value: str | int, color: str) -> str:
    safe_label = str(label).replace(" ", "_").replace("-", "_")
    safe_value = str(value).replace(" ", "_").replace("-", "_")
    return f"https://img.shields.io/badge/{safe_label}-{safe_value}-{color}?style=flat"


def format_pr_links(
    merged: list[dict], open_prs: list[dict], open_prefix: str
) -> str:
    parts: list[str] = []
    for item in merged:
        parts.append(f"[{item['title']}]({item['url']})")
    for item in open_prs:
        parts.append(f"{open_prefix} [{item['title']}]({item['url']})")
    return ", ".join(parts)


def render_repo_line(
    repo: str,
    grouped: dict[str, dict],
    stars: dict[str, int],
    config: dict,
) -> str | None:
    open_prefix = config.get("open_pr_prefix", "open:")
    max_merged = config.get("max_merged_prs_per_repo", 0)
    max_open = config.get("max_open_prs_per_repo", 0)

    merged = sorted(grouped[repo]["merged"], key=lambda item: item["at"] or "")
    open_prs = sorted(grouped[repo]["open"], key=lambda item: item["at"] or "")

    merged = limit_items(merged, max_merged)
    open_prs = limit_items(open_prs, max_open)

    if not merged and not open_prs:
        return None

    pr_text = format_pr_links(merged, open_prs, open_prefix)
    star_count = stars.get(repo, 0)
    tag = ecosystem_tag(repo, config)
    tag_suffix = f" · *{tag}*" if tag else ""

    return (
        f"- **[{repo}](https://github.com/{repo})**{tag_suffix} "
        f"[![GitHub stars]({star_badge_url(star_count)})]"
        f"(https://github.com/{repo}/stargazers) - {pr_text}"
    )


def load_cncf_landscape() -> dict:
    if not CNCF_PATH.exists():
        return {"zones": {}}
    with CNCF_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"zones": {}}


def pr_number_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def format_short_date(iso: str | None) -> str:
    if not iso:
        return "????-??-??"
    return iso[:10]


def format_age(iso: str | None) -> str:
    if not iso:
        return "?"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1d"
    return f"{days}d"


def classify_pr_title(title: str) -> str:
    lower = title.lower().strip()
    patterns = [
        ("fix", r"^(fix[\(:!]|bug[\(:!])"),
        ("docs", r"^(docs[\(:!]|doc[\(:!])"),
        ("ci", r"^(ci[\(:!]|test[\(:!])"),
        ("feat", r"^(feat[\(:!])"),
        ("chore", r"^(chore[\(:!]|refactor[\(:!])"),
    ]
    for label, pattern in patterns:
        if re.search(pattern, lower):
            return label
    if "ci:" in lower or "test:" in lower or "workflow" in lower:
        return "ci"
    if "doc" in lower:
        return "docs"
    return "other"


def all_merged_prs(grouped: dict[str, dict]) -> list[dict]:
    merged: list[dict] = []
    for repo, data in grouped.items():
        for pr in data["merged"]:
            merged.append({**pr, "repo": repo})
    merged.sort(key=lambda item: item.get("at") or "", reverse=True)
    return merged


def all_open_prs(grouped: dict[str, dict]) -> list[dict]:
    open_prs: list[dict] = []
    for repo, data in grouped.items():
        for pr in data["open"]:
            open_prs.append({**pr, "repo": repo})
    open_prs.sort(key=lambda item: item.get("at") or "", reverse=True)
    return open_prs


def fetch_review_stats(token: str, username: str) -> tuple[int, int]:
    query = f"reviewed-by:{username} is:pr"
    try:
        reviews = graphql_search(token, query)
    except RuntimeError:
        return 0, 0

    repos = {item["repository"]["nameWithOwner"] for item in reviews if item}
    return len(reviews), len(repos)


def render_ci_badge(config: dict) -> str:
    repo = config.get("github_repo", "kubeboiii/kubeboiii")
    workflow = config.get("workflow_file", "update-readme.yml")
    badge_url = f"https://github.com/{repo}/actions/workflows/{workflow}/badge.svg"
    actions_url = f"https://github.com/{repo}/actions/workflows/{workflow}"
    return f"[![README CI]({badge_url})]({actions_url})"


def render_cncf_passport(grouped: dict[str, dict], landscape: dict) -> str:
    zones: dict = landscape.get("zones", {}) or {}
    if not zones:
        return ""

    matched_zones: list[tuple[str, list[str]]] = []
    for zone, repos in zones.items():
        hits = [repo for repo in repos if repo in grouped]
        if hits:
            matched_zones.append((zone, hits))

    if not matched_zones:
        return ""

    project_count = len({repo for _, hits in matched_zones for repo in hits})
    zone_count = len(matched_zones)

    lines = [
        "#### Cloud native passport",
        "",
        f"**{project_count} projects** across **{zone_count} landscape zones**",
        "",
    ]
    for zone, hits in matched_zones:
        repo_links = ", ".join(
            f"[{repo}](https://github.com/{repo})" for repo in hits
        )
        lines.append(f"- **{zone}** — {repo_links}")

    return "\n".join(lines)


def render_contributor_mix(grouped: dict[str, dict], config: dict) -> str:
    merged = all_merged_prs(grouped)
    if not merged:
        return ""

    min_prs = config.get("contributor_mix_min_prs", 3)
    if len(merged) < min_prs:
        return ""

    counts: dict[str, int] = defaultdict(int)
    for pr in merged:
        counts[classify_pr_title(pr["title"])] += 1

    total = len(merged)
    order = ["fix", "docs", "ci", "feat", "chore", "other"]
    parts: list[str] = []
    for key in order:
        if counts[key]:
            pct = round(counts[key] * 100 / total)
            parts.append(f"**{key}** {pct}%")

    if not parts:
        return ""

    return (
        "#### Contributor mix\n\n"
        + " · ".join(parts)
        + f" *(based on {total} merged PR titles)*"
    )


def render_ship_log(grouped: dict[str, dict], config: dict) -> str:
    merged = all_merged_prs(grouped)
    if not merged:
        return ""

    limit = config.get("ship_log_limit", 5)
    recent = merged[:limit]

    lines = ["#### Ship log", ""]
    for pr in recent:
        when = format_short_date(pr.get("at"))
        repo = pr["repo"]
        title = pr["title"]
        url = pr["url"]
        lines.append(
            f"- `{when}` **[{repo}](https://github.com/{repo})** — "
            f"[{title}]({url})"
        )

    return "\n".join(lines)


def render_open_pr_pods(grouped: dict[str, dict]) -> str:
    open_prs = all_open_prs(grouped)
    if not open_prs:
        return ""

    lines = [
        "#### Open pull requests",
        "",
        "```text",
        f"{'NAME':<34} {'PHASE':<16} {'AGE':<6}",
    ]
    for pr in open_prs[:10]:
        number = pr_number_from_url(pr["url"])
        name = f"{pr['repo']}#{number}"
        if len(name) > 33:
            name = name[:32] + "…"
        phase = "PendingReview"
        age = format_age(pr.get("at"))
        lines.append(f"{name:<34} {phase:<16} {age:<6}")

    lines.append("```")
    return "\n".join(lines)


def render_reviewer_score(
    token: str, username: str, config: dict
) -> str:
    min_count = config.get("reviewer_min_count", 5)
    review_count, repo_count = fetch_review_stats(token, username)
    if review_count < min_count:
        return ""

    return (
        f"Reviewed **{review_count}** pull requests "
        f"across **{repo_count}** repositories."
    )


def render_narrative(
    grouped: dict[str, dict],
    config: dict,
    landscape: dict,
    token: str,
    username: str,
) -> str:
    sections: list[str] = []

    ci = render_ci_badge(config)
    if ci:
        sections.append(ci)

    cncf = render_cncf_passport(grouped, landscape)
    if cncf:
        sections.append(cncf)

    mix = render_contributor_mix(grouped, config)
    if mix:
        sections.append(mix)

    ship = render_ship_log(grouped, config)
    if ship:
        sections.append(ship)

    pods = render_open_pr_pods(grouped)
    if pods:
        sections.append(pods)

    reviewer = render_reviewer_score(token, username, config)
    if reviewer:
        sections.append(reviewer)

    return "\n\n".join(sections)


def render_stats(grouped: dict[str, dict]) -> str:
    merged_count, open_count = count_prs(grouped)
    repo_count = len(grouped)

    badges = [
        f"![Merged PRs]({static_badge('Merged_PRs', merged_count, '2ea44f')})",
        f"![Open PRs]({static_badge('Open_PRs', open_count, 'fb8500')})",
        f"![Repos]({static_badge('Repos', repo_count, '0969da')})",
    ]
    return "\n".join(badges)


def render_contributions(
    grouped: dict[str, dict], stars: dict[str, int], config: dict
) -> str:
    if not grouped:
        return "_No upstream contributions found yet — check back after your next merged PR!_"

    ordered = sort_repos(grouped, stars, config)
    all_lines: list[str] = []
    for repo in ordered:
        line = render_repo_line(repo, grouped, stars, config)
        if line:
            all_lines.append(line)

    if not all_lines:
        return "_No upstream contributions found yet._"

    return "\n".join(all_lines)


def render_footer() -> str:
    return f"\n*Last updated: {date.today().isoformat()}*"


def build_readme(
    stats: str,
    narrative: str,
    contributions: str,
    footer: str,
) -> str:
    with TEMPLATE_PATH.open(encoding="utf-8") as handle:
        template = handle.read()

    replacements = {
        "<!-- AUTO:STATS -->": stats,
        "<!-- AUTO:NARRATIVE -->": narrative,
        "<!-- AUTO:CONTRIBUTIONS -->": contributions,
        "<!-- AUTO:FOOTER -->": footer,
    }

    output = template
    for anchor, content in replacements.items():
        if anchor not in output:
            if anchor == "<!-- AUTO:CONTRIBUTIONS -->":
                sys.exit("README.template.md is missing <!-- AUTO:CONTRIBUTIONS --> anchor")
            continue
        output = output.replace(anchor, content)

    return output


def main() -> None:
    config = load_config()
    landscape = load_cncf_landscape()
    token = get_token()
    username = config["github_username"]

    grouped = fetch_pull_requests(token, username, config)
    cache = load_cache()
    stars = fetch_stars(token, list(grouped.keys()), cache)

    stats = render_stats(grouped)
    narrative = render_narrative(grouped, config, landscape, token, username)
    contributions = render_contributions(grouped, stars, config)
    footer = render_footer()
    readme = build_readme(stats, narrative, contributions, footer)

    OUTPUT_PATH.write_text(readme, encoding="utf-8")
    merged_count, open_count = count_prs(grouped)
    print(
        f"Wrote {OUTPUT_PATH} ({len(grouped)} repos, "
        f"{merged_count} merged, {open_count} open PRs)"
    )


if __name__ == "__main__":
    main()
