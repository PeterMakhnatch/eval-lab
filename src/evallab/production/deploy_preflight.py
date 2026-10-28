"""Read-only preflight for deploying the Modal cloud dashboard with its data files.

It decides which small retained files the new tabs need on the ``evallab-cloud-store`` Volume,
checks each one, stages clean copies into a local directory, and prints the exact
``modal volume put`` and ``modal deploy`` commands. It never runs Modal, never opens the cloud
trace database (it only reports its size), never edits a source file, and never stages a file on
the never-copy list.

A file is staged only if it exists as a regular file, is within the size the cloud loader accepts,
parses with the cloud loader itself, and carries no per-run label or reward field. The Run Atlas,
Coverage, Field reliability, Task Evidence, Trace Tools, Grader repairs and Cross-test sets tabs
are then reported as ready or as degraded, with the missing file named.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evallab.production import (
    atlas_coverage,
    atlas_view,
    cross_test_sets,
    detector_scope,
    gate_progress,
    harness_view,
    repair_board,
)
from evallab.production.field_reliability import FILE as FIELD_RELIABILITY_FILE
from evallab.production.field_reliability import MAX_BYTES as FIELD_RELIABILITY_BYTES
from evallab.production.field_reliability import load_field_reliability
from evallab.production.tool_scorecard_view import (
    ACCOUNTING_PREFIX,
    ATLAS_DIR,
    ATLAS_SCORECARD_DIR,
    MAX_DOCUMENT_BYTES,
    MAX_STREAM_BYTES,
    RESULTS_FILE,
    SPEND_FILE,
    TOOL_SCORECARD_FILE,
    TOOLS_DIR,
    build_tool_scorecard,
    iter_lines,
    newest_regular,
)

SCHEMA = "evallab.deploy_preflight/v1"
VOLUME_NAME = "evallab-cloud-store"
ENTRY = "src/evallab/production/modal_cloud.py"
DEPLOY = f"modal deploy --strategy recreate {ENTRY}"
DATABASE_FILE = "cloud-trace-db.sqlite"
DEFAULT_STATE_ROOT = Path("~/.local/state/evallab-prod-live")
Policy = Literal["record", "aggregate", "findings"]

#: Per-run label or reward fields. A file holding one is refused.
RECORD_FORBIDDEN = frozenset(
    {
        "label", "labels", "source_label", "is_exploit", "reward", "rewards", "verdict", "gold",
        "ground_truth", "answer_key", "flagged_passage_steps", "is_task_defect_fail",
    }
)
#: Aggregate files (scorecards, the registry) legitimately hold label *counts*; per-run fields are still refused.
AGGREGATE_FORBIDDEN = RECORD_FORBIDDEN - {"label", "labels", "verdict"}
#: Detector findings carry a detector ``verdict`` (alarm, clear, abstain, unknown), not a ground-truth one.
FINDINGS_FORBIDDEN = RECORD_FORBIDDEN - {"verdict"}
_BENCH_LABEL = re.compile(r"(hackbench|cheatbench)[_-]?labels?", re.IGNORECASE)

#: Names that must never be copied to the cloud volume, with why. Matched against file names.
NEVER_COPY: tuple[tuple[str, str], ...] = (
    ("*.sqlite*", "databases: the volume's admitted copy is authoritative; no raw database copy"),
    ("*-calls-v1.jsonl", "model-call ledgers (error text and prompt digests)"),
    ("keymap-v1.jsonl", "run-to-key mapping that can rejoin labels"),
    ("evidence", "working evidence directories"),
    ("cascade-*", "cascade working files and caches"),
    ("laminar-own*", "Laminar working state"),
    ("*manifest*", "sealed and QA manifests"),
    ("*answer-key*", "answer keys"),
    ("assignments.csv", "split assignments (read from the repository image, never from the volume)"),
    ("*label*", "label files"),
)
NEVER_COPY_PLACES = (
    "research/answer-keys/** outside the repository image: labels and sealed manifests",
    "~/.local/state/evallab-cross-test/**: cross-test bundles are local-only, eval-only, no-training, no uploads",
    "any HackBench or CheatBench record, label or run text",
    "cloud-trace-db.sqlite and prod.sqlite: never copied whole; adoption needs an exact allowlisted scope",
)


@dataclass(frozen=True)
class Item:
    key: str
    remote: str
    tabs: tuple[str, ...]
    required: bool
    limit: int
    policy: Policy
    jsonl: bool
    why: str


ITEMS: tuple[Item, ...] = (
    Item("facets", "run-atlas/facets-v1.jsonl", ("Run Atlas", "Coverage", "Task Evidence", "Trace Tools", "Grader repairs"),
         True, atlas_view.MAX_FACET_BYTES, "record", True, "stored whole-run facets"),
    Item("clusters", "run-atlas/clusters-v1.jsonl", ("Run Atlas",),
         False, atlas_view.MAX_CLUSTER_BYTES, "record", True, "cluster tree and run paths"),
    Item("blinding_refresh", f"run-atlas/{atlas_coverage.REFRESH_FILE}", ("Run Atlas", "Coverage"),
         True, atlas_coverage.MAX_REFRESH_BYTES, "record", False, "which runs await re-blinding; without it Run Atlas and Coverage show an error notice"),
    Item("reblinded", f"run-atlas/{atlas_coverage.REBLINDED_FILE}", ("Coverage",),
         False, atlas_coverage.MAX_REBLINDED_BYTES, "record", True, "re-extracted facets to compare with"),
    Item("field_reliability", f"run-atlas/{FIELD_RELIABILITY_FILE}", ("Field reliability",),
         True, FIELD_RELIABILITY_BYTES, "record", False, "measured agreement per facet field"),
    Item("tool_scorecard", f"{TOOLS_DIR}/{TOOL_SCORECARD_FILE}", ("Trace Tools",),
         True, MAX_DOCUMENT_BYTES, "aggregate", False, "per tool and detector counts on labelled cohorts"),
    Item("detector_scope", detector_scope.DEFAULT_FILE, ("Trace Tools",),
         False, MAX_DOCUMENT_BYTES, "aggregate", False, "cascade and Stage 1 DEV counts and the HackBench cross-test counts"),
    Item("gate_progress", gate_progress.DEFAULT_FILE, ("Task Evidence",),
         False, gate_progress.MAX_BYTES, "aggregate", False, "Task QA gate rerun progress: counts per model and arm"),
    Item("harness_findings", harness_view.FINDINGS_FILE, ("Harness",),
         False, harness_view.MAX_FINDINGS_BYTES, "findings", True, "trace-findings/v1 records: every detector's result per run"),
    Item("harness_scorecard", harness_view.SCORECARD_FILE, ("Harness",),
         False, harness_view.MAX_SCORECARD_BYTES, "aggregate", False, "combined scorecard: counts per corpus, detector combination and reader"),
    Item("results", f"{TOOLS_DIR}/{RESULTS_FILE}", ("Trace Tools", "Task Evidence"),
         True, MAX_STREAM_BYTES, "record", True, "per-run tool results behind the run lists"),
    Item("atlas_scorecard", f"{ATLAS_DIR}/{ATLAS_SCORECARD_DIR}/<newest>.json", ("Trace Tools",),
         False, MAX_DOCUMENT_BYTES, "aggregate", False, "facet and cascade detector counts"),
    Item("spend", f"{TOOLS_DIR}/{SPEND_FILE}", ("Trace Tools",),
         False, MAX_STREAM_BYTES, "record", True, "recorded tool spend"),
    Item("accounting", f"{ATLAS_DIR}/{ACCOUNTING_PREFIX}<newest>.jsonl", ("Trace Tools",),
         False, MAX_STREAM_BYTES, "record", True, "recorded facet usage"),
    Item("repair_inventory", repair_board.DEFAULT_FILE, ("Grader repairs",),
         True, repair_board.MAX_BYTES, "record", False, "TS-9 inventory, reduced to the fields the board reads"),
    Item("cross_test_registry", cross_test_sets.DEFAULT_FILE, ("Cross-test sets",),
         True, cross_test_sets.MAX_BYTES, "aggregate", False, "cross-test registry, reduced to the fields the page reads"),
)
#: Tabs, in page order, with every file whose absence changes what they show.
TAB_NEEDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("Run Atlas", ("facets", "blinding_refresh"), ("clusters",)),
    ("Coverage", ("facets", "blinding_refresh"), ("reblinded",)),
    ("Field reliability", ("field_reliability",), ()),
    ("Task Evidence", ("facets", "results"), ("gate_progress",)),
    ("Trace Tools", ("tool_scorecard", "results"), ("atlas_scorecard", "spend", "accounting", "detector_scope")),
    ("Grader repairs", ("repair_inventory",), ("facets",)),
    ("Harness", (), ("harness_findings", "harness_scorecard", "facets")),
)
#: Tabs that also read the cloud trace database on the volume (not staged here).
NEEDS_DATABASE = ("Run Atlas", "Coverage", "Task Evidence", "Trace Tools", "Grader repairs", "Harness")
ENVIRONMENT = (
    ("EVALLAB_CLOUD_TOKEN", "required; Modal secret evallab-cloud-production-auth (not checked here)"),
    ("EVALLAB_REPAIR_INVENTORY", f"optional; default /data/{repair_board.DEFAULT_FILE}"),
    ("EVALLAB_CROSS_TEST_REGISTRY", f"optional; default /data/{cross_test_sets.DEFAULT_FILE}"),
    ("EVALLAB_DETECTOR_SCOPE", f"optional; default /data/{detector_scope.DEFAULT_FILE}"),
    ("EVALLAB_GATE_PROGRESS", f"optional; default /data/{gate_progress.DEFAULT_FILE}"),
)


@dataclass
class _Scan:
    bytes: int = 0
    lines: int = 0
    bad_lines: int = 0
    sha256: str = ""
    forbidden: Counter[str] = field(default_factory=Counter)


def _forbidden(key: str, policy: Policy) -> bool:
    names = {"record": RECORD_FORBIDDEN, "findings": FINDINGS_FORBIDDEN}.get(policy, AGGREGATE_FORBIDDEN)
    return key.lower() in names or bool(_BENCH_LABEL.search(key))


def _walk(value: Any, policy: Policy, found: Counter[str]) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if _forbidden(str(key), policy):
                    found[str(key)] += 1
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


def _stream(source: Path, target: Path, item: Item) -> _Scan:
    """Copy ``source`` to ``target`` while scanning it once; a symlink or non-regular file is refused."""
    scan = _Scan()
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        if item.jsonl:
            for line in iter_lines(source):
                digest.update(line)
                out.write(line)
                scan.bytes += len(line)
                if not line.strip():
                    continue
                scan.lines += 1
                try:
                    _walk(json.loads(line), item.policy, scan.forbidden)
                except ValueError:
                    scan.bad_lines += 1
        else:
            raw = atlas_view.read_regular(source.parent, source.name, item.limit)
            if raw is None:
                raise FileNotFoundError(source)
            digest.update(raw)
            out.write(raw)
            scan.bytes = len(raw)
            _walk(json.loads(raw), item.policy, scan.forbidden)
    scan.sha256 = digest.hexdigest()
    return scan


def _write_document(target: Path, document: dict[str, Any], policy: Policy) -> _Scan:
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    scan = _Scan(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    _walk(document, policy, scan.forbidden)
    return scan


def _regular(path: Path | None) -> os.stat_result | None:
    if path is None:
        return None
    try:
        seen = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return seen if stat.S_ISREG(seen.st_mode) else None


def _sources(state: Path, inventory: Path | None, registry: Path | None) -> dict[str, Path | None]:
    atlas, tools = state / ATLAS_DIR, state / TOOLS_DIR
    scorecards = newest_regular(atlas / ATLAS_SCORECARD_DIR, lambda name: name.endswith(".json"))
    accounting = newest_regular(
        atlas, lambda name: name.startswith(ACCOUNTING_PREFIX) and name.endswith(".jsonl")
    )
    return {
        "facets": atlas / atlas_view.FACETS_FILE,
        "clusters": atlas / atlas_view.CLUSTERS_FILE,
        "blinding_refresh": atlas / atlas_coverage.REFRESH_FILE,
        "reblinded": atlas / atlas_coverage.REBLINDED_FILE,
        "field_reliability": atlas / FIELD_RELIABILITY_FILE,
        "tool_scorecard": tools / TOOL_SCORECARD_FILE,
        "results": tools / RESULTS_FILE,
        "detector_scope": tools / detector_scope.FILE,
        "harness_findings": state / harness_view.FINDINGS_FILE,
        "harness_scorecard": state / harness_view.SCORECARD_FILE,
        "gate_progress": state / gate_progress.DEFAULT_FILE,
        "atlas_scorecard": scorecards[0] if scorecards else None,
        "spend": tools / SPEND_FILE,
        "accounting": accounting[0] if accounting else None,
        "repair_inventory": inventory,
        "cross_test_registry": registry,
    }


def _remote(item: Item, source: Path | None) -> str:
    if item.key == "atlas_scorecard" and source is not None:
        return f"{ATLAS_DIR}/{ATLAS_SCORECARD_DIR}/{source.name}"
    if item.key == "accounting" and source is not None:
        return f"{ATLAS_DIR}/{source.name}"
    return item.remote


def _git(repo: Path, *arguments: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *arguments], capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _tree(path: Path) -> tuple[int, int]:
    """Total bytes and file count under ``path``, not following links."""
    total = count = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        count += 1
        except OSError:
            continue
    return total, count


def _source_report(repo: Path) -> dict[str, Any]:
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain")
    origin = _git(repo, "rev-parse", "origin/main")
    image = {name: _tree(repo / name)[0] for name in ("src", "library", "research", "sql")}
    keys_bytes, keys_files = _tree(repo / "research" / "answer-keys")
    return {
        "repo_root": str(repo),
        "head": head,
        "clean": status == "" if status is not None else None,
        "head_is_origin_main": head is not None and head == origin,
        "image_upload_bytes": image,
        "answer_keys_in_image": {
            "files": keys_files,
            "bytes": keys_bytes,
            "note": (
                "the Modal image uploads all of research/, which includes research/answer-keys; "
                "the corpus registry reads splits/assignments.csv and dataset indices from it for admission. "
                "Deploy from a clean worktree of origin/main so no untracked local file is uploaded."
            ),
        },
    }


def _never_copy_present(state: Path) -> list[str]:
    found: list[str] = []
    for directory in (state, state / ATLAS_DIR, state / TOOLS_DIR):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if any(fnmatch.fnmatch(name, pattern) for pattern, _ in NEVER_COPY):
                found.append(str(directory.relative_to(state) / name) if directory != state else name)
    return found


def _loader_checks(stage: Path, staged: dict[str, str]) -> dict[str, str | None]:
    """Run the cloud loaders on the staged root; a key maps to an error, or None when it loads."""
    errors: dict[str, str | None] = {}
    store = atlas_view.load_atlas_store(stage / ATLAS_DIR)
    if "facets" in staged:
        errors["facets"] = None if store.facets else "no facet record validated"
    if "clusters" in staged:
        errors["clusters"] = store.cluster_error or (None if store.clusters else "no complete cluster build")
    if "blinding_refresh" in staged or "reblinded" in staged:
        refresh = atlas_coverage.load_refresh_files(stage / ATLAS_DIR)
        for key in ("blinding_refresh", "reblinded"):
            if key in staged:
                errors[key] = refresh.error
    if "field_reliability" in staged:
        loaded = load_field_reliability(stage / ATLAS_DIR)
        errors["field_reliability"] = None if loaded["measured"] else loaded["error"]
    if "repair_inventory" in staged:
        loaded = repair_board.load_inventory(stage / repair_board.DEFAULT_FILE)
        errors["repair_inventory"] = None if loaded["present"] else loaded["error"]
    if "cross_test_registry" in staged:
        loaded = cross_test_sets.load_registry(stage / cross_test_sets.DEFAULT_FILE)
        errors["cross_test_registry"] = None if loaded["present"] else loaded["error"]
    if "gate_progress" in staged:
        loaded = gate_progress.load_progress(stage / gate_progress.DEFAULT_FILE)
        errors["gate_progress"] = None if loaded["present"] else loaded["error"]
    if "harness_findings" in staged:
        loaded = harness_view.load_findings(stage / harness_view.FINDINGS_FILE)
        errors["harness_findings"] = loaded["error"] or (None if loaded["by_run"] else "no finding validated")
    if "harness_scorecard" in staged:
        loaded = harness_view.load_scorecard(stage / harness_view.SCORECARD_FILE)
        errors["harness_scorecard"] = None if loaded["present"] else loaded["error"]
    scorecard = build_tool_scorecard(stage)
    by_file = {source["file"]: source["error"] for source in scorecard["sources"]}
    for key in ("tool_scorecard", "results", "atlas_scorecard", "spend", "accounting", "detector_scope"):
        if key in staged:
            name = Path(staged[key]).name
            errors[key] = by_file.get(name, "not read by the Trace Tools loader")
    return errors


def run_preflight(
    state_root: Path,
    *,
    repo_root: Path,
    inventory: Path | None,
    registry: Path | None,
    stage_directory: Path | None = None,
) -> dict[str, Any]:
    """Check, stage and report. Writes only under ``stage_directory`` (or a temporary directory)."""
    state = Path(state_root).expanduser()
    keep = stage_directory is not None
    if stage_directory is not None:
        stage = Path(stage_directory).expanduser()
        if stage.exists() and any(stage.iterdir()):
            raise ValueError(f"{stage} is not empty; stage into a new directory")
        stage.mkdir(parents=True, exist_ok=True)
    else:
        stage = Path(tempfile.mkdtemp(prefix="evallab-deploy-stage-"))
    try:
        return _preflight(state, repo_root, inventory, registry, stage, keep)
    finally:
        if not keep:
            shutil.rmtree(stage, ignore_errors=True)


def _preflight(
    state: Path, repo: Path, inventory: Path | None, registry: Path | None, stage: Path, keep: bool
) -> dict[str, Any]:
    sources = _sources(state, inventory, registry)
    staged: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    for item in ITEMS:
        source = sources[item.key]
        remote = _remote(item, source)
        row: dict[str, Any] = {
            "key": item.key, "remote": remote, "tabs": list(item.tabs), "required": item.required,
            "why": item.why, "status": "missing", "bytes": None, "sha256": None, "detail": None,
        }
        files.append(row)
        seen = _regular(source)
        if source is None or seen is None:
            row["detail"] = "no regular file at the source path" if source is not None else "no source found"
            row["source"] = None if source is None else str(source)
            continue
        row["source"] = str(source)
        if seen.st_size > item.limit:
            row.update(status="invalid", bytes=seen.st_size, detail=f"exceeds the {item.limit}-byte limit the cloud loader accepts")
            continue
        target = stage / remote
        try:
            if item.key == "repair_inventory":
                scan = _write_document(
                    target, repair_board.project_inventory(source.read_bytes()), item.policy
                )
            elif item.key == "cross_test_registry":
                scan = _write_document(
                    target, cross_test_sets.project_registry(source.read_bytes()), item.policy
                )
            else:
                scan = _stream(source, target, item)
        except (OSError, ValueError) as error:
            target.unlink(missing_ok=True)
            row.update(status="invalid", detail=f"does not parse: {error}")
            continue
        row.update(bytes=scan.bytes, sha256=scan.sha256, lines=scan.lines)
        if scan.forbidden:
            target.unlink(missing_ok=True)
            row.update(
                status="refused",
                detail="holds per-run label or reward fields: " + ", ".join(f"{k} x{v}" for k, v in sorted(scan.forbidden.items())),
            )
            continue
        if scan.bad_lines:
            row["warning"] = f"{scan.bad_lines} lines did not parse as JSON; the cloud loader counts and skips them"
        row["status"] = "staged"
        staged[item.key] = remote
    for key, error in _loader_checks(stage, staged).items():
        row = next(entry for entry in files if entry["key"] == key)
        if error:
            row.update(status="invalid", detail=f"the cloud loader rejects it: {error}")
            (stage / row["remote"]).unlink(missing_ok=True)
            staged.pop(key, None)
        else:
            row["status"] = "ok"
    by_key = {entry["key"]: entry for entry in files}
    ok_keys = {key for key, entry in by_key.items() if entry["status"] == "ok"}
    tabs = []
    for name, needs, helps in TAB_NEEDS:
        missing = [key for key in needs if key not in ok_keys]
        lacking = [key for key in helps if key not in ok_keys]
        tabs.append(
            {
                "tab": name,
                "ready": not missing,
                "needs": list(needs),
                "missing": missing,
                "degraded_without": lacking,
                "needs_database": name in NEEDS_DATABASE,
            }
        )
    blockers = [
        f"{entry['key']}: {entry['status']}" + (f" ({entry['detail']})" if entry["detail"] else "")
        for entry in files if entry["required"] and entry["status"] != "ok"
    ]
    database = state / DATABASE_FILE
    seen_db = _regular(database)
    source = _source_report(repo)
    warnings = [
        f"{entry['key']}: {entry['status']} (tabs degrade: {', '.join(entry['tabs'])})"
        for entry in files if not entry["required"] and entry["status"] != "ok"
    ] + [f"{entry['key']}: {entry['warning']}" for entry in files if entry.get("warning")]
    if source["clean"] is not True or not source["head_is_origin_main"]:
        warnings.append("deploy source is not a clean checkout at origin/main: use a clean worktree")
    place = str(stage) if keep else "<STAGE_DIR>"
    puts = [
        f"modal volume put {VOLUME_NAME} {place}/{remote} /{remote}"
        for remote in sorted(staged.values())
    ]
    return {
        "schema": SCHEMA,
        "ok": not blockers,
        "state_root": str(state),
        "stage_directory": str(stage) if keep else None,
        "files": files,
        "tabs": tabs,
        "database": {
            "local_path": str(database),
            "local_bytes": seen_db.st_size if seen_db else None,
            "copied": False,
            "note": f"never copied or opened; the deployed app reads /data/{DATABASE_FILE} on the volume, "
            "whose contents this preflight does not check (it makes no Modal call)",
        },
        "never_copy": {
            "names": [{"pattern": pattern, "why": why} for pattern, why in NEVER_COPY],
            "places": list(NEVER_COPY_PLACES),
            "present_in_state_root_not_staged": _never_copy_present(state),
        },
        "environment": [{"name": name, "note": note} for name, note in ENVIRONMENT],
        "source": source,
        "blockers": blockers,
        "warnings": warnings,
        "commands": {
            "run": False,
            "note": "printed only; run them yourself after Peter approves the shared-service change",
            "modal_cli": shutil.which("modal"),
            "volume_put": puts,
            "deploy": f"cd {repo} && {DEPLOY}",
        },
    }
