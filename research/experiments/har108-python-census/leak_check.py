#!/usr/bin/env python3
"""HAR-108 leak metadata: can the task's fix be downloaded from inside the sandbox?

Code tasks run with ``network_mode = "public"``. The answer-leak blocklist
blocks the git forges and search engines but not PyPI. A FineEnvs code task's
title is usually the upstream GitHub issue or PR title (000226 "Don't use
urlsplit on request path" is Pylons/waitress#260, closed 2019-08-27 by
881fc2b4c3). If a PyPI release of the project was uploaded after that issue
closed, ``pip download <project>`` very likely fetches code containing the fix.

Two network passes, both metadata only ($0), cached under ``--cache``:

1. PyPI JSON API. Candidate names come from the ``split_group`` repo name and
   the modules the hidden tests import (``evallab.task_health``). The match is
   ``repo_url`` when the project's URLs name the split_group repo, and
   ``name`` when a candidate the task title or instruction mentions exists on
   PyPI.
2. GitHub issue search for the task title, restricted to the repo, where the
   repo comes from ``split_group`` or the matched PyPI project's GitHub URL. It
   needs an exact title match and stays under GitHub's 30 searches a minute.

Writes ``pypi.json`` (input to ``evallab tasks health-collect --pypi``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
from evallab.task_health import _excluded_module, project_key_for, static_checks  # noqa: E402

GITHUB_REPO = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
PRIVATE_IMPORT = re.compile(r"^[+ ]\s*(?:from|import)\s+([A-Za-z]\w*)\._\w+", re.M)
NOT_REPO_OWNERS = {"sponsors", "orgs", "user-attachments", "apps", "features", "topics"}
#: Import or directory names that are namespaces, placeholders or PyPI stubs,
#: never the project under test (``sklearn`` on PyPI is a deprecated stub).
GENERIC_NAMES = {
    "google",
    "app",
    "apps",
    "sklearn",
    "src",
    "lib",
    "utils",
    "common",
    "core",
    "main",
    "setup",
    "conftest",
    "test",
    "tests",
    "api",
    "server",
    "client",
    "Lib",
}


def fetch_json(url: str, cache: Path, headers: dict[str, str] | None = None) -> dict | None:
    path = cache / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")
    if path.exists():
        payload = json.loads(path.read_text())
        return payload.get("body")
    request = urllib.request.Request(
        url, headers={"User-Agent": "eval-lab-har108", **(headers or {})}
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
            break
        except urllib.error.HTTPError as error:
            # 404: no such PyPI project; 422: GitHub cannot search that repo
            # (renamed, deleted or private). Both mean "not found".
            if error.code in (404, 422):
                body = None
                break
            if error.code in (403, 429) or error.code >= 500:
                wait = int(error.headers.get("Retry-After") or 0) or 20 * (attempt + 1)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"giving up on {url}")
    path.write_text(json.dumps({"url": url, "body": body}))
    return body


def pypi_facts(name: str, cache: Path) -> dict | None:
    body = fetch_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json", cache)
    if not body or "info" not in body:
        return None
    info = body["info"]
    uploads = {
        version: min(f["upload_time_iso_8601"] for f in files)
        for version, files in (body.get("releases") or {}).items()
        if files
    }
    urls = [info.get("home_page") or "", info.get("download_url") or ""]
    urls += list((info.get("project_urls") or {}).values())
    repos = []
    for url in urls:
        for owner, repo in GITHUB_REPO.findall(url or ""):
            repo = repo.removesuffix(".git").rstrip(".")
            if owner.lower() not in NOT_REPO_OWNERS and f"{owner}/{repo}" not in repos:
                repos.append(f"{owner}/{repo}")
    return {
        "pypi_project": info["name"],
        "latest_version": info.get("version"),
        "last_upload": max(uploads.values()) if uploads else None,
        "releases": len(uploads),
        "uploads": uploads,
        "github_repos": repos,
    }


def repo_name_candidates(repo: str) -> list[str]:
    base = repo.removesuffix(".py")
    names = [base]
    for prefix in ("python-", "py-", "django-"):
        if base.lower().startswith(prefix):
            names.append(base[len(prefix) :])
    for suffix in ("-python", "-py", ".py"):
        if base.lower().endswith(suffix):
            names.append(base[: -len(suffix)])
    return list(dict.fromkeys(names))


def task_facts(entry: dict) -> dict:
    task_dir = (
        ROOT / entry["task"] if not Path(entry["task"]).is_absolute() else Path(entry["task"])
    )
    if not task_dir.exists():
        task_dir = Path.home() / "Developer/eval-lab" / entry["task"]
    meta = tomllib.loads((task_dir / "task.toml").read_text())
    title = (
        (meta.get("metadata") or {}).get("title")
        or (meta.get("task") or {}).get("description")
        or ""
    )
    instruction = (task_dir / "instruction.md").read_text(errors="replace")
    static = static_checks(task_dir)
    key, source = project_key_for(
        entry.get("split_group"),
        entry["task_id"],
        static["imported_modules"],
        static["patch_files"],
    )
    modules = [
        m for m in dict.fromkeys(static["imported_modules"]) if m and not _excluded_module(m)
    ]
    patch = (task_dir / "tests" / "test.patch").read_text(errors="replace")
    private = set(PRIVATE_IMPORT.findall(patch))
    return {
        "title": title,
        "instruction": instruction,
        "key": key,
        "source": source,
        "modules": modules[:4],
        "private": private,
    }


def mentioned(name: str, text: str) -> bool:
    words = {name.lower(), name.lower().replace("_", "-"), name.lower().replace("-", "_")}
    low = text.lower()
    return any(re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", low) for w in words)


def match_pypi(facts: dict, cache: Path) -> dict:
    """Best PyPI project for one task, with how it was matched."""
    split_repo = None
    if facts["source"] == "split_group" and facts["key"].startswith("github.com/"):
        split_repo = "/".join(facts["key"].split("/")[1:3])
        for name in repo_name_candidates(split_repo.split("/")[1]):
            found = pypi_facts(name, cache)
            if found and any(r.lower() == split_repo.lower() for r in found["github_repos"]):
                return {
                    **found,
                    "match": "repo_url",
                    "repo": split_repo,
                    "repo_source": "split_group",
                }
    text = facts["title"] + "\n" + facts["instruction"]
    names = list(facts["modules"])
    if facts["source"] == "test_path":
        names.append(facts["key"])
    for name in dict.fromkeys(names):
        # Corroboration: the task names the module, or the hidden tests import
        # one of its private submodules (only the project's own tests do that).
        if name in GENERIC_NAMES or not (mentioned(name, text) or name in facts["private"]):
            continue
        for candidate in dict.fromkeys([name, name.replace("_", "-")]):
            found = pypi_facts(candidate, cache)
            if found and found["releases"]:
                repo = found["github_repos"][0] if found["github_repos"] else split_repo
                return {
                    **found,
                    "match": "name",
                    "repo": repo,
                    "repo_source": "pypi_urls"
                    if found["github_repos"]
                    else ("split_group" if split_repo else None),
                }
    return {
        "pypi_project": None,
        "match": None,
        "latest_version": None,
        "last_upload": None,
        "releases": 0,
        "uploads": {},
        "github_repos": [],
        "repo": split_repo,
        "repo_source": "split_group" if split_repo else None,
    }


def normal(text: str) -> str:
    """Title identity: case, whitespace and punctuation (backticks, trailing
    periods, quotes) do not distinguish an upstream title from the task's."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class GitHubSearch:
    """Issue/PR title search, paced under the 30-per-minute search limit."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        token = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True
        ).stdout.strip()
        self.headers = {"Accept": "application/vnd.github+json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.last = 0.0

    def canonical_repo(self, repo: str) -> str | None:
        """The repo's current ``owner/name``. Search refuses renamed repos
        (422); the repos API follows the rename redirect."""
        body = fetch_json(f"https://api.github.com/repos/{repo}", self.cache, self.headers)
        return body.get("full_name") if isinstance(body, dict) else None

    def issue(self, repo: str, title: str) -> dict | None:
        repo = self.canonical_repo(repo) or repo
        phrase = re.sub(r'["\\]', " ", title).strip()
        if not phrase:
            return None
        if len(phrase) > 200:
            phrase = phrase[:200].rsplit(" ", 1)[0]
        query = f'repo:{repo} in:title "{phrase}"'
        url = "https://api.github.com/search/issues?per_page=10&q=" + urllib.parse.quote(query)
        cached = (self.cache / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")).exists()
        if not cached:
            time.sleep(max(0.0, self.last + 2.2 - time.time()))
            self.last = time.time()
        body = fetch_json(url, self.cache, self.headers) or {}
        for item in body.get("items") or []:
            if normal(item.get("title") or "") == normal(title):
                pull = item.get("pull_request") or {}
                return {
                    "number": item["number"],
                    "is_pr": bool(pull),
                    "state": item.get("state"),
                    "closed_at": pull.get("merged_at") or item.get("closed_at"),
                    "url": item.get("html_url"),
                }
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=Path, default=HERE / "pool.json")
    parser.add_argument("--out", type=Path, default=HERE / "pypi.json")
    parser.add_argument("--cache", type=Path, default=Path("/private/tmp/har108/leak-cache"))
    parser.add_argument("--no-github", action="store_true")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    pool = json.loads(args.pool.read_text())["pool"]
    with ThreadPoolExecutor(16) as workers:
        facts = list(workers.map(task_facts, pool))
        matches = list(workers.map(lambda f: match_pypi(f, args.cache), facts))
    print(f"pypi: {sum(bool(m['match']) for m in matches)}/{len(pool)} matched", flush=True)
    search = None if args.no_github else GitHubSearch(args.cache)
    tasks = {}
    for n, (entry, fact, match) in enumerate(zip(pool, facts, matches, strict=True)):
        issue = search.issue(match["repo"], fact["title"]) if search and match["repo"] else None
        closed = issue["closed_at"] if issue else None
        after = sorted(
            (up, version) for version, up in match["uploads"].items() if closed and up > closed
        )
        tasks[entry["task_id"]] = {
            "title": fact["title"],
            "pypi_project": match["pypi_project"],
            "match": match["match"],
            "latest_version": match["latest_version"],
            "last_upload": match["last_upload"],
            "releases": match["releases"],
            "repo_url": f"https://github.com/{match['repo']}" if match["repo"] else None,
            "repo_source": match["repo_source"],
            "issue": issue,
            "released_after_close": (bool(after) if match["pypi_project"] else None)
            if closed
            else None,
            "first_release_after_close": after[0][1] if after else None,
        }
        if search and n % 100 == 99:
            print(f"github: {n + 1}/{len(pool)}", flush=True)
    args.out.write_text(
        json.dumps(
            {
                "card": "HAR-108",
                "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "sources": [
                    "https://pypi.org/pypi/<name>/json",
                    "https://api.github.com/search/issues",
                ],
                "tasks": tasks,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"wrote {args.out} ({len(tasks)} tasks)")


if __name__ == "__main__":
    main()
