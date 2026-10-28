"""Mine pinned, task-local changes without executing untrusted upstream code.

A touched test is only a candidate. Publication requires two-family classification;
merge commits and newly introduced tasks are not counted as repairs.
"""

from __future__ import annotations

import ast
import csv
import fnmatch
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from evallab.failure_library.fixmine.models import Candidate
from evallab.failure_library.records import Split


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_repo(repo: str) -> tuple[str, str]:
    """Return an HTTPS source URL and collision-resistant owner/repository slug."""
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    parsed = urlparse(repo)
    if parsed.scheme == "file" or (not parsed.scheme and Path(repo).exists()):
        path = Path(parsed.path if parsed.scheme else repo).resolve()
        return path.as_uri(), "local-" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query:
        raise ValueError("repo must be an HTTPS Git URL (or a local fixture repository)")
    path = parsed.path.rstrip("/").removesuffix(".git")
    if not path or parsed.fragment:
        raise ValueError("repo URL needs a repository path, without a fragment")
    url = f"https://{parsed.netloc.lower()}{path}"
    material = path.strip("/").lower()
    if parsed.hostname != "github.com":
        material = parsed.netloc.lower() + "/" + material
    slug = re.sub(r"[^a-z0-9._-]+", "-", material).strip("-")
    return url, slug


def git(repo: Path, *args: str) -> bytes:
    # Partial clones can have a commit-graph entry before its promised object is
    # hydrated. Consult the object database, not that optional derived index.
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "-c",
            "core.commitGraph=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        check=True,
        timeout=600,
    )
    return result.stdout


def clone_repo(repo_url: str, state_root: Path, *, refresh: bool = False) -> Path:
    url, slug = canonical_repo(repo_url)
    target = state_root / "raw" / "git" / slug
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--", url, str(target)],
            capture_output=True,
            check=True,
            timeout=900,
        )
    stored = git(target, "remote", "get-url", "origin").decode().strip()
    if canonical_repo(stored)[0] != url:
        raise ValueError(f"cached clone {target} belongs to a different repository")
    if refresh:
        git(target, "fetch", "--filter=blob:none", "origin")
    return target


def immutable_write(path: Path, data: bytes) -> None:
    """Never overwrite source evidence; the same address must mean the same bytes."""
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable cache conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o444)


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def protected_tasks(assignments: Path | None) -> dict[str, Split]:
    """Conservatively keep any existing non-DEV task out of the knowledge feed."""
    result: dict[str, Split] = {}
    if assignments is None:
        return result
    with assignments.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            task = row["task"]
            split: Split = "DEV" if row["split"].lower() == "dev" else "cross_test"
            if result.get(task) != "cross_test":
                result[task] = split
    return result


def task_split(repo_slug: str, family: str, task_id: str, protected: dict[str, Split]) -> Split:
    if task_id in protected:
        return protected[task_id]
    group = f"fixmine/v1\x1f{repo_slug}\x1f{family}"
    bucket = int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5
    return "heldout" if bucket == 0 else "DEV"


def _glob_root(path: str, pattern: str) -> str | None:
    """A task glob can name directories (tasks/*) or files (.../*/*.json)."""
    parts, glob_parts = path.split("/"), pattern.strip("/").split("/")
    if "**" in glob_parts:
        if fnmatch.fnmatchcase(path, pattern):
            return path
        return None
    if len(parts) < len(glob_parts):
        return None
    if all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, glob_parts, strict=False)):
        return "/".join(parts[: len(glob_parts)])
    return None


def _relevant(path: str) -> bool:
    text = path.lower()
    name = Path(path).name.lower()
    return (
        any(
            token in text
            for token in (
                "test",
                "verif",
                "grader",
                "solution",
                "answer",
                "ground_truth",
                "ground-truth",
                "evaluator",
            )
        )
        or name in {"grade.py", "tasks.json"}
        or name.startswith("tasks_")
        or ("evaluation_examples/" in text and name.endswith(".json"))
        or "config_files/" in text
    )


def _json_items(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return {}
    if isinstance(data, dict):
        if "id" in data or "task_id" in data:
            data = [data]
        elif data and all(isinstance(value, dict) for value in data.values()):
            return {str(key): value for key, value in data.items()}
        else:
            return {}
    if not isinstance(data, list):
        return {}
    result: dict[str, Any] = {}
    for item in data:
        if not isinstance(item, dict):
            return {}
        key = item.get("id", item.get("task_id", item.get("task_index")))
        if key is None or str(key) in result:
            return {}
        result[str(key)] = item
    return result


def _python_tasks(text: str) -> dict[str, tuple[str, str]]:
    """Parse tau-bench Task lists with AST only, never import upstream code."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    result: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if not names or not names[0].startswith("TASKS"):
            continue
        partition = names[0].removeprefix("TASKS").strip("_").lower() or "tasks"
        for index, task in enumerate(node.value.elts):
            if not isinstance(task, ast.Call):
                continue
            user = next(
                (
                    kw.value.value
                    for kw in task.keywords
                    if kw.arg == "user_id" and isinstance(kw.value, ast.Constant)
                ),
                index,
            )
            result[f"{partition}:{index}"] = (ast.dump(task, include_attributes=False), str(user))
    return result


def _read_blob(repo: Path, revision: str, path: str) -> str:
    return git(repo, "show", f"{revision}:{path}").decode("utf-8", errors="replace")


def _changed_tasks(
    repo: Path, parent: str, revision: str, path: str, root: str
) -> list[tuple[str, str, str]]:
    """Return (id, task path, family), excluding additions and unchanged rows."""
    if root != path:
        task_id = Path(root).name
        family = re.sub(r"(?:[-_](?:v\d+|easy|medium|hard|\d+))+$", "", task_id)
        return [(task_id, root, family)]
    before, after = _read_blob(repo, parent, path), _read_blob(repo, revision, path)
    if path.endswith((".json", ".jsonl")):
        old_items, new_items = _json_items(before), _json_items(after)
        # A failed row mapping must not turn one shared file into a fake task id.
        if not old_items or not new_items:
            return []
        domain = Path(path).parent.name
        return [
            (key, f"{path}#{key}", domain + ":" + re.sub(r"[_-]\d+$", "", key))
            for key in sorted(old_items.keys() & new_items.keys())
            if old_items[key] != new_items[key]
        ]
    if Path(path).name.startswith("tasks") and path.endswith(".py"):
        old_tasks, new_tasks = _python_tasks(before), _python_tasks(after)
        # Positional ids are not stable across insertions/reorders. Do not
        # manufacture claims by comparing two different tasks at the same index.
        if old_tasks.keys() != new_tasks.keys():
            return []
        domain = Path(path).parent.name
        return [
            (f"{domain}:{key}", f"{path}#{key}", f"{domain}:{old_tasks[key][1]}")
            for key in sorted(old_tasks.keys() & new_tasks.keys())
            if old_tasks[key][0] != new_tasks[key][0] and old_tasks[key][1] == new_tasks[key][1]
        ]
    return [(Path(path).stem, path, Path(path).parent.as_posix())]


def _task_diff(repo: Path, parent: str, revision: str, paths: list[str], task_path: str) -> bytes:
    """For shared task tables, isolate the changed row instead of labeling its neighbours."""
    if "#" in task_path:
        path, key = task_path.rsplit("#", 1)
        old, new = _read_blob(repo, parent, path), _read_blob(repo, revision, path)
        if path.endswith((".json", ".jsonl")):
            before, after = _json_items(old)[key], _json_items(new)[key]
            import difflib

            return "".join(
                difflib.unified_diff(
                    json.dumps(before, indent=2, sort_keys=True).splitlines(True),
                    json.dumps(after, indent=2, sort_keys=True).splitlines(True),
                    fromfile=f"a/{task_path}",
                    tofile=f"b/{task_path}",
                )
            ).encode()
        before, after = _python_tasks(old)[key][0], _python_tasks(new)[key][0]
        return f"--- a/{task_path}\n+++ b/{task_path}\n-{before}\n+{after}\n".encode()
    return git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--binary",
        parent,
        revision,
        "--",
        *paths,
    )


def mine_repository(
    repo_url: str,
    task_glob: str,
    state_root: Path,
    *,
    since: str | None = None,
    refresh: bool = False,
    assignments: Path | None = None,
    license_id: str | None = None,
) -> tuple[Path, list[Candidate]]:
    """Snapshot all qualifying non-merge changes at the cached upstream HEAD."""
    if since is not None:
        date.fromisoformat(since)
    url, slug = canonical_repo(repo_url)
    repo = clone_repo(url, state_root, refresh=refresh)
    revision = git(repo, "rev-parse", "origin/HEAD" if refresh else "HEAD").decode().strip()
    protected = protected_tasks(assignments)
    query = {
        "schema_version": "fixmine/v1",
        "repo": url,
        "revision": revision,
        "task_glob": task_glob,
        "since": since,
        "license": license_id,
        "split_assignments_digest": digest(assignments.read_bytes()) if assignments else None,
    }
    mine_root = state_root / "fixmine" / slug / hashlib.sha256(json_bytes(query)).hexdigest()[:16]
    candidate_path = mine_root / "candidates.jsonl"
    if candidate_path.exists():
        manifest = json.loads((mine_root / "manifest.json").read_text())
        if digest(candidate_path.read_bytes()) != manifest["candidates_digest"]:
            raise ValueError("candidate cache digest mismatch")
        return mine_root, [
            Candidate.model_validate_json(line) for line in candidate_path.read_text().splitlines()
        ]
    args = ["log", "--no-merges", "--format=%H%x00%P%x00%cs%x00%B%x1e", revision]
    if since:
        args.append(f"--since={since}T00:00:00+00:00")
    args += ["--", f":(glob){task_glob}/**", f":(glob){task_glob}"]
    commits = git(repo, *args).decode("utf-8", errors="replace").split("\x1e")
    candidates: list[Candidate] = []
    inspected = 0
    for record in commits:
        if not record.strip():
            continue
        commit, parents, committed, message = record.lstrip("\n").split("\x00", 3)
        if len(parents.split()) != 1:
            continue
        parent = parents.strip()
        inspected += 1
        names = (
            git(
                repo,
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "--no-renames",
                parent,
                commit,
            )
            .decode()
            .splitlines()
        )
        groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for line in names:
            status, path = line.split("\t", 1)
            root = _glob_root(path, task_glob)
            if status not in {"M", "A", "D"} or root is None or not _relevant(path):
                continue
            if status != "M":
                if root == path:
                    continue
                # Adding a new test can repair an existing weak verifier, but
                # introducing an entirely new task is not evidence of a fix.
                probe_revision = parent if status == "A" else commit
                try:
                    git(repo, "cat-file", "-e", f"{probe_revision}:{root}")
                except subprocess.CalledProcessError:
                    continue
            for key in _changed_tasks(repo, parent, commit, path, root):
                groups[key].append(path)
        for (task_id, task_path, family), paths in sorted(groups.items()):
            paths = sorted(set(paths))
            patch = _task_diff(repo, parent, commit, paths, task_path)
            if not patch:
                continue
            patch_digest = digest(patch)
            diff_ref = "diffs/" + patch_digest.removeprefix("sha256:") + ".patch"
            immutable_write(mine_root / diff_ref, patch)
            candidate_id = hashlib.sha256(
                f"{url}\x1f{parent}\x1f{commit}\x1f{task_path}\x1f{patch_digest}".encode()
            ).hexdigest()[:24]
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    repo_url=url,
                    repo_slug=slug,
                    parent_revision=parent,
                    fix_revision=commit,
                    task_id=task_id,
                    task_path=task_path,
                    family=family,
                    changed_files=paths,
                    diff_digest=patch_digest,
                    diff_ref=diff_ref,
                    commit_message=message.strip(),
                    committed_at=date.fromisoformat(committed),
                    split=task_split(slug, family, task_id, protected),
                    license=license_id,
                )
            )
    candidates.sort(key=lambda row: row.candidate_id)
    payload = b"".join(json_bytes(row.model_dump(mode="json")) for row in candidates)
    immutable_write(candidate_path, payload)
    immutable_write(
        mine_root / "manifest.json",
        json_bytes(
            {
                **query,
                "commits_inspected": inspected,
                "candidate_count": len(candidates),
                "candidates_digest": digest(payload),
            }
        ),
    )
    return mine_root, candidates
