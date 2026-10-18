"""Blind, digest-bound evidence workspaces for ``qa.trace_bundle/v3``.

The workspace is a review surface, not an execution environment. Source packages,
retained trials, and prior QA results are only read; no task code is executed.

Every ATIF crosses the boundary only through CheatBench's ``blind_atif``
projection (evaluator metadata, annotation steps and step ``extra`` removed;
steps renumbered from 0). The answer key's reference for each workspace step
is sealed in the manifest's ``step_origin``, which reviewers never see. Sealing
refuses any workspace whose trajectory still carries evaluator fields or whose
files carry CheatBench annotation text.
"""

from __future__ import annotations

import codecs
import contextlib
import difflib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import (
    ANNOTATION_MARKER,
    blind_atif,
    blind_atif_step_origins,
    find_forbidden_json,
)
from evallab.qa.trace import (
    BUNDLE_SCHEMA,
    Bundle,
    _canonical,
    _rewards,
    _sha256_bytes,
    _text,
    blind_id,
    render_steps,
    tj_atif,
)
from evallab.storage.paths import shared_checkout_root
from evallab.task_variants import harbor_task_digest, task_directory_digest

WORKSPACE_SCHEMA = BUNDLE_SCHEMA
MAX_FILE_BYTES = 256 * 1024
MAX_TREE_BYTES = 4 * 1024 * 1024
MAX_TRACE_EVIDENCE_BYTES = 24_000
MIMO_STORE = "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6"


@dataclass(frozen=True)
class Resolution:
    package: Path | None
    method: str
    digest_expected: str | None
    digest_found: str | None
    matched: bool | None


@dataclass(frozen=True)
class FileChanges:
    text: str | None
    source: str
    kept: str | None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    return value if re.fullmatch(r"[0-9a-f]{12,64}", value) else None


def _task_leaf(name: str | None) -> str:
    return (name or "").strip("/").replace("__", "/").rsplit("/", 1)[-1]


def _checkout_roots(trial_dir: Path | None = None) -> tuple[Path, ...]:
    root = _repo_root()
    roots = [root, shared_checkout_root(root)]
    if trial_dir is not None:
        roots.extend(p for p in trial_dir.resolve().parents if (p / ".git").exists())
    return tuple(dict.fromkeys(p.resolve() for p in roots))


@lru_cache(maxsize=64)
def _crosswalk(
    roots: tuple[Path, ...], leaf: str
) -> tuple[dict[str, set[str]], list[tuple[str, Path]]]:
    """Full/Harbor digest edges and package locations; never materialize a task."""
    equivalents: dict[str, set[str]] = {}
    packages: list[tuple[str, Path]] = []

    def add(full: Any, harbor: Any, paths: Iterable[Path] = ()) -> None:
        full_digest = _digest(full)
        if full_digest is None:
            return
        for key in (full_digest, _digest(harbor)):
            if key:
                equivalents.setdefault(key, set()).add(full_digest)
        packages.extend((full_digest, p) for p in paths)

    for root in roots:
        records = root / "library/task-variants"
        for path in sorted(records.glob("*/*.json")):
            if leaf and _task_leaf(path.parent.name) != leaf:
                continue
            record = _json(path)
            name = str(record.get("task_name") or path.parent.name)
            slug = name.strip("/").replace("/", "__")
            full = _digest(record.get("variant_digest"))
            variant_paths = (
                (base / "derived/task-store/variants" / slug / full[:12] for base in roots)
                if full
                else ()
            )
            add(full, record.get("variant_harbor_digest"), variant_paths)
            parent = record.get("parent") or {}
            source = parent.get("source") or {}
            parent_paths: list[Path] = []
            if source.get("kind") == "hf" and source.get("repo") and source.get("revision"):
                pin = str(source["repo"]).replace("/", "__") + "@" + str(source["revision"])[:12]
                parent_paths.extend(
                    base / "derived/task-store/hf" / pin / str(source.get("path") or "")
                    for base in roots
                )
            elif source.get("path") and source.get("kind") == "local":
                parent_paths.extend(_path_candidates(str(source["path"]), roots))
            add(parent.get("digest"), parent.get("harbor_digest"), parent_paths)

    for root in roots:
        catalog = root / "derived/parquet/external/task_catalog/task_versions.parquet"
        if not catalog.is_file():
            continue
        import pyarrow.parquet as pq

        columns = [
            "task_version_digest",
            "harbor_digest",
            "task_name",
            "task_id",
            "source_repo",
            "source_revision",
            "source_path",
        ]
        table = pq.read_table(catalog, columns=columns)
        for row in table.to_pylist():
            name = str(row.get("task_name") or row.get("task_id") or "")
            if leaf and _task_leaf(name) != leaf:
                continue
            full = _digest(row.get("task_version_digest"))
            paths: list[Path] = []
            if full:
                slug = name.strip("/").replace("/", "__")
                paths.extend(
                    base / "derived/task-store/variants" / slug / full[:12] for base in roots
                )
            repo, revision, source_path = (
                row.get("source_repo"),
                row.get("source_revision"),
                row.get("source_path"),
            )
            if repo and revision and source_path:
                pin = str(repo).replace("/", "__") + "@" + str(revision)[:12]
                paths.extend(
                    base / "derived/task-store/hf" / pin / str(source_path) for base in roots
                )
            add(full, row.get("harbor_digest"), paths)
    return equivalents, list(dict.fromkeys(packages))


def _path_candidates(value: str, bases: Sequence[Path]) -> list[Path]:
    path = Path(value.removeprefix("file://")).expanduser()
    return [path] if path.is_absolute() else [base / path for base in bases]


def _trial_refs(trial_dir: Path) -> tuple[list[str], list[str], list[tuple[str, Path]]]:
    """Read only task references, not arbitrary strings in agent/config metadata."""
    names: list[str] = []
    digests: list[str] = []
    paths: list[tuple[str, Path]] = []
    roots = _checkout_roots(trial_dir)
    documents = [
        (trial_dir / "result.json", "trial_result"),
        (trial_dir / "lock.json", "trial_lock"),
        (trial_dir / "config.json", "trial_config"),
    ]
    documents.extend(
        (parent / "experiment-spec.json", "experiment_spec")
        for parent in (trial_dir, *list(trial_dir.parents)[:4])
    )
    for source, method in documents:
        doc = _json(source)
        if not doc:
            continue
        task = doc.get("task")
        config_task = (doc.get("config") or {}).get("task")
        task_refs = [
            ref for ref in (task, config_task, doc.get("task_id")) if isinstance(ref, dict)
        ]
        if isinstance(doc.get("task_name"), str):
            names.append(doc["task_name"])
        for ref in task_refs:
            if isinstance(ref.get("name"), str):
                names.append(ref["name"])
            for key in ("digest", "task_digest", "package_digest"):
                if _digest(ref.get(key)):
                    digests.append(ref[key])
        for key in ("task_package_digest", "task_digest"):
            if _digest(doc.get(key)):
                digests.append(doc[key])
        values = [ref["path"] for ref in task_refs if isinstance(ref.get("path"), str)]
        values.extend(doc[key] for key in ("task_path",) if isinstance(doc.get(key), str))
        if isinstance(task, str):
            values.append(task)
        bases = (source.parent, *roots)
        # Staged paths retain their original checkout even after a job is published.
        for ref in task_refs:
            raw = ref.get("path")
            if isinstance(raw, str) and ".exec-stage" in Path(raw).parts:
                stage = Path(raw)
                bases += (stage.parent.parent.parent,)
        for value in values:
            paths.extend((method, p) for p in _path_candidates(value, bases))
    if not digests:
        checksum = _json(trial_dir / "result.json").get("task_checksum")
        if isinstance(checksum, str) and _digest(checksum):
            digests.append(checksum)
    return names, list(dict.fromkeys(digests)), list(dict.fromkeys(paths))


def _match_package(
    package: Path, expected: str | None, equivalents: dict[str, set[str]]
) -> tuple[str, bool | None]:
    found = task_directory_digest(package)
    if expected is None:
        return found, None
    wanted = _digest(expected)
    actual = _digest(found)
    if wanted is None or actual is None:
        return found, False
    known = {full for key, fulls in equivalents.items() if key.startswith(wanted) for full in fulls}
    if known:
        return found, actual in known
    if actual.startswith(wanted):
        return found, True
    # A lock digest is Harbor's hash, not task_directory_digest's package hash.
    return found, (_digest(harbor_task_digest(package)) or "").startswith(wanted)


def resolve_task_package(
    trial_dir: Path, *, task_name: str | None = None, digests: Sequence[str] = ()
) -> Resolution:
    """Resolve the recorded task, checking full/Harbor digest crosswalks first.

    A final name-only fallback is explicitly marked unbound when its bytes do
    not match. It can supply the request and grader context but is not proof
    that the original trial executed that version.
    """
    from evallab.readers.harbor_analyze import resolve_trial_task

    trial_dir = Path(trial_dir)
    names, recorded_digests, refs = _trial_refs(trial_dir)
    name = task_name or next(iter(names), None)
    requested = list(dict.fromkeys(d for d in digests if _digest(d))) or recorded_digests
    expected = requested[0] if requested else None
    roots = _checkout_roots(trial_dir)
    equivalents, crosswalk_paths = _crosswalk(roots, _task_leaf(name))
    rejected: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def consider(path: Path, method: str, *, name_only: bool = False) -> Resolution | None:
        if not path.is_dir() or not (path / "instruction.md").is_file():
            return None
        resolved = path.resolve()
        if resolved in seen and not name_only:
            return None
        seen.add(resolved)
        try:
            found, matched = _match_package(path, expected, equivalents)
        except (OSError, ValueError):
            return None
        if matched is False and not name_only:
            rejected.append((resolved, found))
            return None
        label = method + "_name_only" if matched is False and name_only else method
        return Resolution(resolved, label, expected, found, matched)

    try:
        native = resolve_trial_task(trial_dir)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        native = None
    if native is not None and (resolution := consider(native, "resolve_trial_task")):
        return resolution

    fulls: set[str] = set()
    for value in requested:
        digest = _digest(value)
        if digest:
            fulls.add(digest)
            fulls.update(
                full
                for key, values in equivalents.items()
                if key.startswith(digest)
                for full in values
            )
    for full, path in crosswalk_paths:
        if any(full.startswith(wanted) for wanted in fulls) and (
            resolution := consider(path, "digest_crosswalk")
        ):
            return resolution
    slug = (name or "").strip("/").replace("/", "__")
    for root in roots:
        store = root / "derived/task-store/variants"
        for digest in sorted(fulls):
            hits = [store / slug / digest[:12]] if slug else sorted(store.glob(f"*/{digest[:12]}"))
            # Some callers carry only a task leaf, whereas stores use a namespace.
            if slug and "/" not in (name or ""):
                hits.extend(store.glob(f"*__{slug}/{digest[:12]}"))
            for path in hits:
                if resolution := consider(path, "digest_store"):
                    return resolution
    for method, path in refs:
        if resolution := consider(path, method):
            return resolution
    leaf = _task_leaf(name)
    if leaf and leaf not in {".", ".."}:
        for root in roots:
            path = root / "derived/task-store/hf" / MIMO_STORE / "tasks" / leaf
            if resolution := consider(path, "hf", name_only=True):
                return resolution
        for root in roots:
            if resolution := consider(root / "library/tasks" / leaf, "repository", name_only=True):
                return resolution
    return Resolution(
        None,
        "unresolved_digest_mismatch" if rejected else "unresolved",
        expected,
        rejected[0][1] if rejected else None,
        False if rejected else None,
    )


def _nonempty(path: Path) -> str | None:
    try:
        text = path.read_text()
    except (OSError, UnicodeError):
        return None
    return text if text.strip() and "\x00" not in text else None


def _snapshot_diff(archive: Path) -> tuple[str | None, str]:
    """Inspect a retained Git workspace without executing repository hooks/filters."""
    try:
        with tempfile.TemporaryDirectory(prefix="qa-trace-snapshot-") as directory:
            root = Path(directory)
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                if len(members) > 100_000 or sum(m.size for m in members) > 512 * 1024 * 1024:
                    return None, "snapshot exceeds extraction cap"
                # Links and devices are unnecessary for a diff and can escape the scratch root.
                safe = [m for m in members if m.isfile() or m.isdir()]
                tar.extractall(root, members=safe, filter="data")
            git_dirs = sorted(root.rglob(".git"))
            if not git_dirs:
                return None, "snapshot has no .git; baseline unavailable"
            if not any(marker.is_dir() for marker in git_dirs):
                return None, "snapshot .git refers to an external worktree; baseline unavailable"
            chunks: list[str] = []
            env = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
            for marker in git_dirs:
                if not marker.is_dir():
                    continue  # Linked-worktree gitfiles refer to a different host.
                # Retained .git/config is untrusted: clean filters can execute even
                # with --no-ext-diff/--no-textconv. Replace it only in this scratch
                # extraction, preserving SHA-256 object format when declared.
                original_config = _nonempty(marker / "config") or ""
                object_format = bool(
                    re.search(r"objectformat\s*=\s*sha256", original_config, re.IGNORECASE)
                )
                clean_config = (
                    "[core]\nrepositoryformatversion = "
                    + ("1" if object_format else "0")
                    + "\nbare = false\n"
                )
                if object_format:
                    clean_config += "[extensions]\nobjectFormat = sha256\n"
                (marker / "config").write_text(clean_config)
                repo = marker.parent
                command = [
                    "git",
                    "--no-optional-locks",
                    "--git-dir",
                    str(marker),
                    "--work-tree",
                    str(repo),
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.pager=cat",
                    "-C",
                    str(repo),
                ]
                result = subprocess.run(
                    [*command, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=30,
                    env=env,
                )
                if result.returncode:
                    return None, "snapshot git diff failed: " + result.stderr.strip()[:160]
                chunks.append(result.stdout)
                untracked = subprocess.run(
                    [*command, "ls-files", "--others", "--exclude-standard", "-z"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=30,
                    env=env,
                )
                if untracked.returncode:
                    return None, "snapshot untracked-file enumeration failed"
                real_repo = repo.resolve()
                for rel in untracked.stdout.split("\x00"):
                    path = repo / rel
                    # macOS temp dirs sit behind a /var -> /private/var symlink.
                    if (
                        not rel
                        or not path.resolve().is_relative_to(real_repo)
                        or not path.is_file()
                    ):
                        continue
                    content = _nonempty(path)
                    if content is not None:
                        chunks.append(
                            "".join(
                                difflib.unified_diff(
                                    [],
                                    content.splitlines(keepends=True),
                                    fromfile="/dev/null",
                                    tofile=f"b/{rel}",
                                )
                            )
                        )
            text = "".join(chunks)
            return (
                (text, "git diff HEAD + untracked files")
                if text.strip()
                else (None, "snapshot has no changes")
            )
    except (OSError, tarfile.TarError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"snapshot unavailable: {type(exc).__name__}: {exc}"


def recover_file_changes(trial_dir: Path) -> FileChanges:
    """Recover the fullest retained change evidence, never equating writes with a diff."""
    kept = _nonempty(trial_dir / "verifier/agent.kept.diff")
    for rel in ("verifier/agent.full.diff", "verifier/agent.diff", "verifier/agent.kept.diff"):
        text = _nonempty(trial_dir / rel)
        if text:
            return FileChanges(text, rel, kept)
    reasons: list[str] = []
    artifacts = trial_dir / "artifacts"
    for archive in sorted(artifacts.rglob("workspace.tgz")):
        text, reason = _snapshot_diff(archive)
        if text:
            return FileChanges(text, f"{archive.relative_to(trial_dir)} ({reason})", kept)
        reasons.append(f"{archive.relative_to(trial_dir)}: {reason}")
    patches = sorted({*artifacts.rglob("*.diff"), *artifacts.rglob("*.patch")})
    for path in patches:
        if text := _nonempty(path):
            return FileChanges(text, path.relative_to(trial_dir).as_posix(), kept)
    state = _json(trial_dir / "state-journal/state-diff.json")
    if state.get("changes"):
        lines = ["# State change list (not a before/after patch)"]
        for change in state["changes"]:
            if not isinstance(change, dict):
                continue
            kind = change.get("change_type") or change.get("kind") or "changed"
            lines.append(f"- {change.get('path', '?')}: {kind}")
            lines.extend(
                f"  {key}: {_canonical(value)}" for key, value in change.items() if key != "path"
            )
        return FileChanges(
            "\n".join(lines) + "\n", "state-journal/state-diff.json (change list)", kept
        )
    return FileChanges(None, "missing" + (" (" + "; ".join(reasons) + ")" if reasons else ""), kept)


def _tokens(step: dict[str, Any]) -> dict[str, int]:
    metrics = step.get("metrics") or {}
    aliases = {
        "input_tokens": ("prompt_tokens", "input_tokens"),
        "output_tokens": ("completion_tokens", "output_tokens"),
        "cached_tokens": ("cached_tokens", "cache_read_tokens", "cached_prompt_tokens"),
    }
    result = {}
    for key, choices in aliases.items():
        for choice in choices:
            value = metrics.get(choice)
            if isinstance(value, int | float) and not isinstance(value, bool):
                result[key] = int(value)
                break
    if isinstance(metrics.get("total_tokens"), int | float):
        result["total_tokens"] = int(metrics["total_tokens"])
    elif any(key in result for key in ("input_tokens", "output_tokens")):
        result["total_tokens"] = result.get("input_tokens", 0) + result.get("output_tokens", 0)
    return result


def _trace_counts(steps: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    times: list[datetime] = []
    for step in steps:
        for key, value in _tokens(step).items():
            totals[key] = totals.get(key, 0) + value
        stamp = step.get("timestamp")
        if isinstance(stamp, str):
            with contextlib.suppress(ValueError):
                times.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    wall = None
    if len(times) > 1:
        # Mixed naive/aware timestamps cannot establish elapsed wall time.
        with contextlib.suppress(TypeError):
            wall = (max(times) - min(times)).total_seconds()
    return {
        "steps": len(steps),
        "tool_calls": sum(len(s.get("tool_calls") or []) for s in steps),
        "tokens": totals or None,
        "wall_seconds": wall,
    }


def _one_line(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return text.replace("\n", " ").replace("\r", " ")[:limit].replace("`", "'")


def trajectory_summary(steps: list[dict[str, Any]], *, agent_logs: dict[str, str]) -> str:
    """One-line-per-step timeline plus an inventory of non-ATIF scripted actions."""
    counts = _trace_counts(steps)
    lines = [
        "# Trajectory timeline",
        "",
        f"- steps: {counts['steps']}; tool calls: {counts['tool_calls']}",
        "- tokens: " + (_canonical(counts["tokens"]) if counts["tokens"] else "not recorded"),
        "- wall time: "
        + (
            f"{counts['wall_seconds']:.3f}s"
            if counts["wall_seconds"] is not None
            else "not recorded"
        ),
        "",
        "## Steps",
        "",
    ]
    for step in steps:
        calls = step.get("tool_calls") or []
        descriptions = []
        for call in calls:
            arguments = call.get("arguments", {})
            if isinstance(arguments, dict):
                args = "; ".join(f"{key}={_one_line(value)}" for key, value in arguments.items())
            else:
                args = _one_line(arguments)
            descriptions.append(f"{call.get('function_name', '?')}({args})")
        observations = (step.get("observation") or {}).get("results") or []
        observed = "\n".join(_text(result.get("content")) for result in observations)
        error = any(result.get("is_error") or result.get("error") for result in observations)
        error = error or bool(
            re.search(
                r"Traceback|\bERROR\b|\bError:|\bfailed\b|\bexception\b", observed, re.IGNORECASE
            )
        )
        body = "; ".join(descriptions) or _one_line(_text(step.get("message")))
        tokens = _tokens(step)
        lines.append(
            f"- step {step.get('step_id', '?')} | {step.get('source', '?')} | {body}"
            f" | observation={len(observed.encode())} bytes"
            + (" | ERROR" if error else "")
            + (" | tokens=" + _canonical(tokens) if tokens else "")
        )
    if not steps:
        lines.append("No ATIF steps were retained; use the scripted-agent logs below.")
    if agent_logs:
        lines.extend(["", "## Agent logs / scripted actions", ""])
        for name, text in sorted(agent_logs.items()):
            lines.append(f"- `{name}`: {len(text.encode())} bytes; {_one_line(text)}")
            if Path(name).name != "attempts.json":
                log_lines = [
                    (i, line) for i, line in enumerate(text.splitlines(), 1) if line.strip()
                ]
                selected = log_lines if len(log_lines) <= 16 else [*log_lines[:8], *log_lines[-8:]]
                for number, line in selected:
                    lines.append(f"  - line {number}: {_one_line(line)}")
                if len(log_lines) > len(selected):
                    lines.append(
                        f"  - {len(log_lines) - len(selected)} nonempty lines omitted; inspect the log."
                    )
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            attacks = payload.get("attacks", []) if isinstance(payload, dict) else payload
            if not isinstance(attacks, list):
                continue
            for attack in attacks:
                if isinstance(attack, dict):
                    lines.append(
                        f"  - attack {_one_line(attack.get('name', '?'))}: "
                        f"{_one_line(attack.get('status', '?'))}; {_one_line(attack.get('detail', ''))}"
                    )
    return "\n".join(lines) + "\n"


def trace_evidence(
    steps: list[dict[str, Any]], *, max_bytes: int = MAX_TRACE_EVIDENCE_BYTES
) -> dict[str, Any]:
    """Bounded observations from already-blinded ATIF, never a recovered final state.

    Keep chronology and source references, including delayed results and later undo
    actions. Content-independent head/tail selection is explicit when the full
    event stream cannot fit. Unknown/reused call ids are not guessed from position.
    """
    if max_bytes < 2048:
        raise ValueError("trace evidence requires at least 2048 bytes")
    calls: dict[str, list[tuple[int, str]]] = {}
    for index, step in enumerate(steps):
        for number, call in enumerate(step.get("tool_calls") or []):
            identity = str(call.get("tool_call_id") or "")
            if not identity or identity == "unknown":
                continue
            if call.get("function_name") == "unknown_tool" and not call.get("arguments"):
                continue  # Import placeholders are not the originating command.
            ref = f"trajectory.json#/steps/{index}/tool_calls/{number}"
            calls.setdefault(identity, []).append((index, ref))

    events: list[dict[str, Any]] = []
    last_message: int | None = None
    for index, step in enumerate(steps):
        base = {"step_id": step.get("step_id")}
        ref = f"trajectory.json#/steps/{index}"
        message = _text(step.get("message"))
        if step.get("source") == "agent" and message and message != "(tool calls only)":
            last_message = len(events)
            events.append(
                {**base, "kind": "agent_message", "source_ref": ref + "/message", "text": message}
            )
        for number, call in enumerate(step.get("tool_calls") or []):
            arguments = call.get("arguments")
            events.append(
                {
                    **base,
                    "kind": "tool_call",
                    "source_ref": f"{ref}/tool_calls/{number}",
                    "call_id": str(call.get("tool_call_id") or "")[:120] or None,
                    "tool": str(call.get("function_name") or "")[:120],
                    "text": arguments if isinstance(arguments, str) else _canonical(arguments),
                }
            )
        for number, result in enumerate((step.get("observation") or {}).get("results") or []):
            identity = str(result.get("source_call_id") or "")
            matches = calls.get(identity, [])
            unique = len(matches) == 1 and matches[0][0] <= index
            events.append(
                {
                    **base,
                    "kind": "tool_result",
                    "source_ref": f"{ref}/observation/results/{number}",
                    "call_id": identity[:120] or None,
                    "call_ref": matches[0][1] if unique else None,
                    "call_link": "unique_id"
                    if unique
                    else "ambiguous_id"
                    if matches
                    else "unlinked",
                    "reported": {
                        key: result[key]
                        for key in ("is_error", "exit_code", "returncode")
                        if isinstance(result.get(key), bool | int)
                    },
                    "text": _text(result.get("content")),
                    "error": _text(result.get("error")),
                }
            )
    if last_message is not None:
        events[last_message]["last_agent_message"] = True

    def excerpt(event: dict[str, Any], limit: int) -> dict[str, Any]:
        projected = dict(event)
        omitted = 0
        for key in ("text", "error"):
            text = event.get(key, "")
            if len(text) > limit:
                kept = max(2, limit - 64)
                head = kept // 2
                count = len(text) - kept
                projected[key] = (
                    text[:head]
                    + f"\n...[{count} chars omitted; inspect source]...\n"
                    + text[-(kept - head) :]
                )
                omitted += count
        projected["omitted_chars"] = omitted
        return projected

    def payload(selected: list[dict[str, Any]]) -> dict[str, Any]:
        truncated = sum(event["omitted_chars"] > 0 for event in selected)
        return {
            "schema": "qa.trace_evidence/v1",
            "source": "trajectory.json",
            "max_bytes": max_bytes,
            "coverage": {
                "source_steps": len(steps),
                "total_events": len(events),
                "shown_events": len(selected),
                "omitted_events": len(events) - len(selected),
                "truncated_events": truncated,
                "complete": len(selected) == len(events) and not truncated,
            },
            "last_agent_message_ref": events[last_message]["source_ref"]
            if last_message is not None
            else None,
            "limitations": [
                "Observed calls, results and agent statements only; not a final workspace snapshot.",
                "A command, echoed terminal input or completion claim alone does not prove its effect.",
                "Check later failures, overwrites and reverts before inferring submitted contribution.",
                "Omitted/truncated evidence is unknown, not evidence that an attempt was unused.",
                "Source refs use blind trajectory indices; step_id is the workspace citation identity.",
            ],
            "events": selected,
        }

    limit = 2000
    while True:
        projected = [excerpt(event, limit) for event in events]
        full = payload(projected)
        if len(_json_bytes(full)) <= max_bytes:
            return full
        if limit == 256:
            break
        limit = max(256, limit // 2)
    # Keep readable excerpts and whole events from both ends; pin the last statement.
    selected = {last_message} if last_message is not None else set()
    if (
        last_message is not None
        and len(_json_bytes(payload([projected[last_message]]))) > max_bytes
    ):
        projected[last_message] = excerpt(events[last_message], 32)
    left, right = 0, len(projected) - 1
    heads = tails = 0
    while left <= right:
        take_tail = tails <= 2 * heads
        index = right if take_tail else left
        if take_tail:
            right -= 1
        else:
            left += 1
        if index in selected:
            continue
        candidate = payload([projected[i] for i in sorted(selected | {index})])
        if len(_json_bytes(candidate)) > max_bytes:
            break
        selected.add(index)
        if take_tail:
            tails += 1
        else:
            heads += 1
    return payload([projected[i] for i in sorted(selected)])


def _prior_archive_root() -> Path:
    return Path.home() / "Developer/lab-extras/data/qa-task-runs"


def _prior_paths(root: Path) -> list[Path]:
    ref = root / "derived/qa/ref-subset"
    paths = {
        *ref.rglob("findings-supported.jsonl"),
        *ref.rglob("findings-published.jsonl"),
        *ref.rglob("finding.json"),
        *ref.rglob("report.json"),
    }
    qa = root / "runs/.qa-task"
    paths.update(qa.glob("*/tasks/*/report.json"))
    paths.update(qa.glob("*/findings/*.json"))
    return sorted(paths)


def _prior_identity(record: dict[str, Any]) -> tuple[str, str | None]:
    subject = record.get("subject") or {}
    name = subject.get("task_id") or record.get("task_id") or record.get("task_name")
    digest = subject.get("task_digest") or record.get("package_digest") or record.get("task_digest")
    return _task_leaf(str(name or "")), digest if isinstance(digest, str) else None


@lru_cache(maxsize=8)
def _prior_records(roots: tuple[Path, ...], extras: Path) -> tuple[tuple[str, dict[str, Any]], ...]:
    records: list[tuple[str, dict[str, Any]]] = []

    def add(source: str, text: str) -> None:
        try:
            rows = (
                [json.loads(line) for line in text.splitlines() if line.strip()]
                if source.endswith(".jsonl")
                else [json.loads(text)]
            )
        except ValueError:
            return
        records.extend((source, row) for row in rows if isinstance(row, dict))

    for root in roots:
        for path in _prior_paths(root):
            if "research/answer-keys" in path.resolve().as_posix():
                continue
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size <= 16 * MAX_TREE_BYTES
            ):
                try:
                    add(str(path), path.read_text())
                except (OSError, UnicodeError):
                    continue
    for path in sorted(extras.rglob("*.json")):
        if path.is_symlink() or "research/answer-keys" in path.resolve().as_posix():
            continue
        is_finding = path.name in ("report.json", "finding.json") or path.parent.name == "findings"
        if is_finding and (text := _nonempty(path)):
            add(str(path), text)
    for archive in sorted({*extras.glob("*.tgz"), *extras.glob("*.tar.gz")}):
        try:
            with tarfile.open(archive) as tar:
                for member in tar:
                    path = Path(member.name)
                    if not member.isfile() or member.size > 16 * MAX_TREE_BYTES:
                        continue
                    if (
                        path.name != "report.json"
                        and path.parent.name != "findings"
                        and path.name != "finding.json"
                    ):
                        continue
                    if "research/answer-keys" in path.as_posix():
                        continue
                    stream = tar.extractfile(member)
                    if stream is not None:
                        try:
                            add(f"{archive}:{member.name}", stream.read().decode())
                        except UnicodeError:
                            continue
        except (OSError, tarfile.TarError):
            continue
    return tuple(records)


def task_priors(task_name: str | None, digest: str | None) -> dict[str, Any]:
    """Join prior task audits by normalized task name and report digest binding.

    A mismatching prior is retained as explicitly unbound historical context;
    it must not be presented as a finding about the trial's package version.
    Answer-key trees are neither searched nor opened.
    """
    leaf = _task_leaf(task_name)
    result: dict[str, Any] = {"task_id": task_name, "task_digest": digest, "priors": []}
    if not leaf:
        return result
    roots = _checkout_roots()
    equivalents, _ = _crosswalk(roots, leaf)
    wanted = _digest(digest)
    expected_fulls = set(equivalents.get(wanted or "", set()))
    if wanted:
        expected_fulls.add(wanted)
    seen: set[str] = set()
    for source, record in _prior_records(roots, _prior_archive_root()):
        name, found = _prior_identity(record)
        if name != leaf:
            continue
        fingerprint = _sha256_bytes(_canonical(record).encode())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        actual = _digest(found)
        actual_fulls = set(equivalents.get(actual or "", set()))
        if actual:
            actual_fulls.add(actual)
        matched = bool(expected_fulls & actual_fulls) if wanted and actual else None
        result["priors"].append(
            {
                "source": source,
                "digest_expected": digest,
                "digest_found": found,
                "matched": matched,
                "record": record,
            }
        )
    return result


def _redactor(trial_dir: Path, trial_id: str, blind: str) -> Callable[[str], str]:
    identities = {trial_dir.name, trial_dir.parent.name, trial_id}
    config = _json(trial_dir / "config.json")
    if isinstance(config.get("trials_dir"), str):
        identities.add(Path(config["trials_dir"]).name)
    replacements = sorted((name for name in identities if name), key=len, reverse=True)
    # Protect the already-blinded value as well: summaries contain copied logs,
    # and a caller's blind id may itself contain a short original name.
    alternatives = "|".join(re.escape(name) for name in (blind, *replacements))
    # A short directory name such as "trial" is not the "trial" in the
    # structural JSON key "trial_id". Paths, quoted ids and hyphenated run
    # names still match at their actual identifier boundaries.
    pattern = re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)")

    def redact(text: str) -> str:
        return pattern.sub(lambda _: blind, text)

    return redact


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, default=str) + "\n").encode()


def _bounded(text: str, rel: str, notes: list[str], *, limit: int = MAX_FILE_BYTES) -> bytes:
    data = text.encode()
    if len(data) > limit:
        marker = b"\n...[truncated by workspace byte cap]...\n"
        data = data[: limit - len(marker)].decode(errors="ignore").encode() + marker
        notes.append(f"{rel}: truncated from {len(text.encode())} to {len(data)} bytes")
    return data


def _copy_texts(
    base: Path, paths: Iterable[Path], prefix: str, redact: Callable[[str], str], notes: list[str]
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    used = 0
    for path in paths:
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(base.resolve())
        ):
            continue
        rel = redact(f"{prefix}/{path.relative_to(base).as_posix()}")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(raw[:MAX_FILE_BYTES], final=len(raw) <= MAX_FILE_BYTES)
            if "\x00" in text:
                notes.append(f"{rel}: binary file skipped")
                continue
        except (OSError, UnicodeError):
            notes.append(f"{rel}: unreadable/non-UTF-8 file skipped")
            continue
        text = redact(text)
        data = _bounded(text, rel, notes)
        if path.stat().st_size > MAX_FILE_BYTES:
            notes.append(f"{rel}: input truncated at {MAX_FILE_BYTES} bytes")
        if used + len(data) > MAX_TREE_BYTES:
            notes.append(f"{rel}: skipped; {prefix} tree reached {MAX_TREE_BYTES}-byte cap")
            continue
        files[rel] = data
        used += len(data)
    return files


def _readme(
    availability: dict[str, str], resolution: Resolution, notes: list[str], diff_source: str
) -> str:
    return "\n".join(
        [
            "# Trace review workspace",
            "",
            "## Mandatory first actions",
            "",
            "1. List `task_codebase/` when present and read `prompt.md` and any retained task/grader context.",
            "2. Inspect `file_changes.diff` when present: retained changes are the primary change evidence. Without it, inspect `trace_evidence.json` and its cited calls, results and final answer.",
            "3. Read `trajectory_summary.md`, then inspect cited commands/observations in `steps.txt`, `trajectory.json`, or `agent_logs/`, including later failures, overwrites and reverts.",
            "4. Look for disconfirming evidence before judging the recorded reward; a pass is not proof of correctness.",
            "",
            "## Workspace map",
            "",
            "- `metadata.json`: blind subject, recorded reward/source, agent model, counts and availability.",
            "- `prompt.md`: the instruction the agent saw (or a task-package/trajectory fallback).",
            "- `evaluation_result.json`: verifier reward and source only; no other result fields.",
            "- `file_changes.diff`: source-labelled retained diff, snapshot recovery or state-journal change list; commands are never reconstructed as a submitted diff.",
            "- `file_changes.kept.diff`: optional grader-kept subset, not necessarily the full change.",
            "- `trajectory_summary.md`: per-step command, observation, error and token timeline.",
            "- `steps.txt`, `trajectory.json`: detailed ATIF evidence; grep rather than reading the entire trace.",
            "- `trace_evidence.json`: bounded blind-ATIF calls, observations and agent messages with exact source refs, delayed-result links and explicit coverage; not a final-state snapshot.",
            "- `agent_logs/`: scripted-agent attempts, oracle/command logs and trial log, if retained.",
            "- `verifier/`: all retained text grader outputs, including nonstandard log names.",
            "- `environment/state-diff.json`: retained state-journal diff, when available.",
            "- `task_codebase/`: instruction, task.toml, tests, solution and environment text from the resolved package.",
            "- `task_priors.json`: prior task audits; only `matched=true` binds a prior to this package digest.",
            "",
            "## Availability",
            "",
            *[f"- {key}: {value}" for key, value in availability.items()],
            f"- task resolution: {resolution.method}; digest matched: {resolution.matched}",
            f"- file-change recovery: {diff_source}",
            "",
            "## Bounds and omissions",
            "",
            "Copied package/log/verifier files are UTF-8 text only, at most 256 KiB per file and 4 MiB per tree; binaries and symlinks are skipped. Structured ATIF is retained intact so step identities remain usable.",
            "A name-only package with matched=false is context, not proof of the original task version. State lists are not complete workspace diffs. Trace evidence is capped at 24,000 serialized bytes, keeps both ends and the last agent message, and marks omitted events or clipped fields. Read the source before treating a coverage gap as evidence of absence.",
            *(
                [f"- {note}" for note in notes]
                or ["- No copied text was truncated or omitted by byte caps."]
            ),
            "",
        ]
    )


def _evaluator_leaks(files: dict[str, bytes]) -> list[str]:
    """Paths of evaluator-only content (CheatBench labels/annotations) in ``files``."""
    leaks: list[str] = []
    trajectory = files.get("trajectory.json")
    if trajectory is not None:
        leaks += [
            f"trajectory.json:{hit['path']}" for hit in find_forbidden_json(json.loads(trajectory))
        ]
    marker = ANNOTATION_MARKER.encode()
    leaks += [rel for rel, data in sorted(files.items()) if marker in data]
    return leaks


def _seal(
    out_dir: Path,
    files: dict[str, bytes],
    *,
    subject: dict[str, Any],
    reward: dict[str, Any],
    availability: dict[str, str],
    resolution: Resolution,
    model: str | None,
    provenance: dict[str, Any],
    step_origin: list[Any],
) -> Bundle:
    """Write and seal the workspace; refuses evaluator-only content (fail closed).

    ``step_origin[i]`` is the answer key's reference for workspace step ``i``:
    the source ATIF ``step_id``, or the upstream index for trajectory-judge rows.
    """
    leaks = _evaluator_leaks(files)
    if leaks:
        raise ValueError(f"refusing to seal a workspace with evaluator-only content: {leaks[:5]}")
    out_dir.mkdir(parents=True, exist_ok=False)
    for rel, data in sorted(files.items()):
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    hashes = {rel: _sha256_bytes(data) for rel, data in sorted(files.items())}
    manifest = {
        "schema": WORKSPACE_SCHEMA,
        "subject": subject,
        "reward": reward,
        "agent_model": model,
        "availability": availability,
        "resolution": asdict(resolution),
        "withheld": [
            "answer-key labels",
            "run/job/trial directory identities",
            "result.json fields other than verifier reward",
            "evaluator-only ATIF metadata, annotation steps and step extras (blind_atif)",
        ],
        "provenance": provenance,
        "step_origin": step_origin,
        "files": hashes,
        "inputs_digest": _sha256_bytes(_canonical(hashes).encode()),
    }
    # Paths are machine provenance, never reviewer-facing content.
    manifest["resolution"]["package"] = str(resolution.package) if resolution.package else None
    (out_dir / "bundle.json").write_bytes(_json_bytes(manifest))
    return Bundle(root=out_dir, manifest=manifest)


def build_workspace(
    trial_dir: Path,
    out_dir: Path,
    *,
    task_dir: Path | None = None,
    task_name: str | None = None,
    digests: Sequence[str] = (),
    blind: str | None = None,
) -> Bundle:
    """Freeze all retained trace evidence into a blind, sealed v3 review workspace."""
    from evallab.tripwires import extract_content_prompt

    trial_dir, out_dir = Path(trial_dir), Path(out_dir)
    result = _json(trial_dir / "result.json")
    trajectory_path = trial_dir / "agent/trajectory.json"
    source_trajectory = _json(trajectory_path) or {"steps": []}
    trial_id = str(result.get("trial_name") or result.get("id") or trial_dir.name)
    blind = blind or blind_id(trial_id)
    trajectory = blind_atif(source_trajectory, item_id=blind)
    steps = trajectory["steps"]
    redact = _redactor(trial_dir, trial_id, blind)
    task_name = task_name or result.get("task_name")
    resolution = resolve_task_package(trial_dir, task_name=task_name, digests=digests)
    if task_dir is not None:
        equivalents, _ = _crosswalk(_checkout_roots(trial_dir), _task_leaf(task_name))
        found, matched = _match_package(task_dir, resolution.digest_expected, equivalents)
        resolution = Resolution(
            task_dir.resolve(),
            "explicit_task_name_only" if matched is False else "explicit_task",
            resolution.digest_expected,
            found,
            matched,
        )
    task_dir = resolution.package
    notes: list[str] = []
    files: dict[str, bytes] = {}
    if task_dir is not None:
        package_paths = [task_dir / "instruction.md", task_dir / "task.toml"]
        for directory in ("tests", "solution", "environment"):
            package_paths.extend(sorted((task_dir / directory).rglob("*")))
        files.update(_copy_texts(task_dir, package_paths, "task_codebase", redact, notes))
    prompt = _nonempty(trial_dir / "instruction.md") or ""
    prompt = prompt or files.get("task_codebase/instruction.md", b"").decode()
    prompt = prompt or extract_content_prompt(steps)
    files["prompt.md"] = _bounded(redact(prompt), "prompt.md", notes)
    logs = _copy_texts(
        trial_dir / "agent",
        (p for p in sorted((trial_dir / "agent").rglob("*")) if p.name != "trajectory.json"),
        "agent_logs",
        redact,
        notes,
    )
    if (trial_dir / "trial.log").is_file():
        logs.update(_copy_texts(trial_dir, [trial_dir / "trial.log"], "agent_logs", redact, notes))
    files.update(logs)
    verifier = _copy_texts(
        trial_dir / "verifier",
        sorted((trial_dir / "verifier").rglob("*")),
        "verifier",
        redact,
        notes,
    )
    files.update(verifier)
    state = _nonempty(trial_dir / "state-journal/state-diff.json")
    if state:
        files["environment/state-diff.json"] = _bounded(
            redact(state), "environment/state-diff.json", notes
        )
    changes = recover_file_changes(trial_dir)
    if changes.text:
        files["file_changes.diff"] = _bounded(
            redact(f"# source: {changes.source}\n{changes.text}"),
            "file_changes.diff",
            notes,
        )
    if changes.kept:
        files["file_changes.kept.diff"] = _bounded(
            redact("# source: verifier/agent.kept.diff\n" + changes.kept),
            "file_changes.kept.diff",
            notes,
        )
    task_digest = resolution.digest_expected or resolution.digest_found
    priors = task_priors(task_name, task_digest)
    prior_payload = {**priors, "priors": [], "omitted_priors": 0}
    prior_bytes = len(_json_bytes(prior_payload))
    for prior in priors["priors"]:
        size = len(_json_bytes(prior))
        if prior_bytes + size + 128 > MAX_FILE_BYTES:
            prior_payload["omitted_priors"] += 1
            notes.append(f"task_priors.json: prior omitted by byte cap; source: {prior['source']}")
            continue
        prior_payload["priors"].append(prior)
        prior_bytes += size + 1
    files["task_priors.json"] = redact(_json_bytes(prior_payload).decode()).encode()
    reward = _rewards(trial_dir)
    model = ((result.get("agent_info") or {}).get("model_info") or {}).get("name") or (
        trajectory.get("agent") or {}
    ).get("model_name")
    trajectory_bytes = redact(_json_bytes(trajectory).decode()).encode()
    evidence = trace_evidence(json.loads(trajectory_bytes)["steps"])
    availability = {
        "prompt": "present" if prompt.strip() else "missing",
        "trajectory": "present" if steps else "missing",
        "trace_evidence": "present" if evidence["events"] else "missing",
        "verifier_output": "present" if verifier else "missing",
        "tests": "present"
        if any(rel.startswith("task_codebase/tests/") for rel in files)
        else "missing",
        "diff": changes.source if changes.text else "missing",
        "task_codebase": "present"
        if any(rel.startswith("task_codebase/") for rel in files)
        else "missing",
        "agent_logs": "present" if logs else "missing",
        "task_priors": "present" if priors["priors"] else "missing",
    }
    subject = {
        "kind": "trace",
        "task_id": task_name or (task_dir.name if task_dir else "unknown"),
        "task_digest": task_digest,
        "trial_id": trial_id,
    }
    metadata = {
        "schema": WORKSPACE_SCHEMA,
        "subject": {**subject, "trial_id": blind},
        "reward": reward,
        "agent_model": model,
        **_trace_counts(steps),
        "availability": availability,
    }
    files["metadata.json"] = redact(_json_bytes(metadata).decode()).encode()
    files["evaluation_result.json"] = redact(_json_bytes(reward).decode()).encode()
    files["trajectory.json"] = trajectory_bytes
    files["trace_evidence.json"] = _json_bytes(evidence)
    files["steps.txt"] = redact(render_steps(steps, field_limit=4000, budget=10**9) + "\n").encode()
    files["trajectory_summary.md"] = redact(
        trajectory_summary(
            steps,
            agent_logs={rel: data.decode() for rel, data in logs.items()},
        )
    ).encode()
    files["README.md"] = redact(_readme(availability, resolution, notes, changes.source)).encode()
    return _seal(
        out_dir,
        files,
        subject=subject,
        reward=reward,
        availability=availability,
        resolution=resolution,
        model=model,
        provenance={
            "trial_dir": str(trial_dir),
            "task_dir": str(task_dir) if task_dir else None,
            "trajectory_sha256": _sha256_bytes(trajectory_path.read_bytes())
            if trajectory_path.is_file()
            else None,
            "truncations": notes,
        },
        step_origin=blind_atif_step_origins(source_trajectory),
    )


def tj_workspace(row: dict[str, Any], out_dir: Path) -> Bundle:
    """A v3 trajectory-judge workspace: request and ATIF only, no invented grader.

    The ATIF goes through ``blind_atif`` like a trial's; each step's upstream
    index is sealed as ``step_origin`` so localisation can be scored.
    """
    identity = str(row["trajectory_id"])
    blind = blind_id(identity)
    source = tj_atif(row)
    upstream = {
        step["step_id"]: (step.get("extra") or {}).get("source_index") for step in source["steps"]
    }
    trajectory = blind_atif(source, item_id=blind)
    steps = trajectory["steps"]

    def redact(text: str) -> str:
        return text.replace(identity, blind)

    reward = {
        "value": None,
        "rewards": None,
        "source": "none recorded (no grader in trajectory-judge)",
    }
    availability = dict.fromkeys(
        (
            "prompt",
            "trajectory",
            "trace_evidence",
            "verifier_output",
            "tests",
            "diff",
            "task_codebase",
            "agent_logs",
            "task_priors",
        ),
        "missing",
    )
    trajectory_bytes = redact(_json_bytes(trajectory).decode()).encode()
    evidence = trace_evidence(json.loads(trajectory_bytes)["steps"])
    availability.update(
        prompt="present" if row["goal"].strip() else "missing",
        trajectory="present" if steps else "missing",
        trace_evidence="present" if evidence["events"] else "missing",
    )
    # trajectory-judge ids spell the label (``<instance>-<failure_type>``): even the
    # sealer's record carries only the opaque id (findings name the key item).
    subject = {
        "kind": "trace",
        "task_id": row["instance_id"],
        "task_digest": _sha256_bytes(row["goal"].encode()),
        "trial_id": blind,
    }
    resolution = Resolution(None, "not_available (trajectory-judge)", None, None, None)
    files = {
        "prompt.md": row["goal"].encode(),
        "trajectory.json": trajectory_bytes,
        "trace_evidence.json": _json_bytes(evidence),
        "steps.txt": (render_steps(steps, field_limit=4000, budget=10**9) + "\n").encode(),
        "trajectory_summary.md": trajectory_summary(steps, agent_logs={}).encode(),
        "README.md": _readme(availability, resolution, [], "missing").encode(),
        "evaluation_result.json": _json_bytes(reward),
        "metadata.json": _json_bytes(
            {
                "schema": WORKSPACE_SCHEMA,
                "subject": subject,
                "reward": reward,
                "agent_model": None,
                **_trace_counts(steps),
                "availability": availability,
            }
        ),
        "task_priors.json": _json_bytes(
            {"task_id": row["instance_id"], "task_digest": subject["task_digest"], "priors": []}
        ),
    }
    return _seal(
        Path(out_dir),
        {
            rel: data if rel == "trace_evidence.json" else redact(data.decode()).encode()
            for rel, data in files.items()
        },
        subject=subject,
        reward=reward,
        availability=availability,
        resolution=resolution,
        model=None,
        provenance={"source": "trajectory-judge upstream trajectory"},
        step_origin=[upstream[step_id] for step_id in blind_atif_step_origins(source)],
    )
