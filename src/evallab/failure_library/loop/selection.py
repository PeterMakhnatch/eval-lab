"""Frozen DEV canaries; labels are retained for scoring, never model inputs."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from importlib import import_module
from pathlib import Path
from typing import Any

from evallab.failure_library.records import TIER_ORDER, Finding, RunRecord
from evallab.failure_library.store import LibraryStore

SCHEMA = "failure-library.canary/v1"
SEED = "failure-library-canary/v1"
HACK_LABELS = frozenset({"reward_hacked", "hacked", "exploit-pass", "reward_hacking"})
CLEAN_LABELS = frozenset({"clean", "actually_solved", "not_hacked"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def bare_task(value: str) -> str:
    return value.rsplit("/", 1)[-1].split("@", 1)[0]


def _rank(value: str) -> str:
    return hashlib.sha256(f"{SEED}\0{value}".encode()).hexdigest()


def split_index(root: Path) -> tuple[dict[tuple[str, str], str], set[str]]:
    """Strict cross-key check: a reported bare task name vetoes DEV exposure."""
    path = root / "research/answer-keys/splits/assignments.csv"
    if not path.is_file():
        raise ValueError(f"split assignments are missing: {path}")
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assignments = {(r["key_id"], r["item_id"]): r["split"].casefold() for r in rows}
    denied = {bare_task(r["task"]) for r in rows if r["split"].casefold() != "dev"}
    return assignments, denied


def _non_dev_task(task: dict[str, Any], denied: set[str]) -> bool:
    task_id = task["task_id"]
    if task["source_id"].startswith("impossible-"):
        task_id = task_id.rsplit("/", 1)[0]
    return bare_task(task_id) in denied


def _key_rows(root: Path) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    for path in sorted((root / "research/answer-keys").glob("*/items.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append((path.parent.name, json.loads(line)))
    return rows


def _task_identity(task: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    return (
        task["source_id"],
        task["task_id"],
        task.get("upstream_revision"),
        task.get("task_version_digest"),
    )


def _key_task(row: dict[str, Any]) -> str:
    return str((row.get("subject") or {}).get("task_id", ""))


def _binding(finding: Finding, keys: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Only an exact key digest match can supply a package path to a finding."""
    matches = [
        (key, row)
        for key, row in keys
        if bare_task(_key_task(row)) == bare_task(finding.task.task_id)
        and finding.task.task_version_digest
        and (row.get("subject") or {}).get("task_digest") == finding.task.task_version_digest
    ]
    for key, row in sorted(
        matches, key=lambda pair: (pair[1].get("item_id") != finding.locator, pair[0])
    ):
        package = row.get("package") or {}
        if package.get("root_hint") and package.get("name"):
            return {
                "package_path": str(Path(package["root_hint"]).expanduser() / package["name"]),
                "key_id": key,
                "key_item_id": row.get("item_id"),
            }
    return {"package_path": finding.task.task_path}


def _controls(
    keys: list[tuple[str, dict[str, Any]]],
    assignments: dict[tuple[str, str], str],
    denied: set[str],
) -> list[dict[str, Any]]:
    controls = []
    for key, row in keys:
        subject = row.get("subject") or {}
        identity = str(row.get("item_id") or "")
        if (
            subject.get("kind") != "task"
            or row.get("expected_verdict") != "pass"
            or str(row.get("split", "")).casefold() != "dev"
            or assignments.get((key, identity)) != "dev"
            or bare_task(_key_task(row)) in denied
            or not subject.get("task_digest")
        ):
            continue
        package = row.get("package") or {}
        source = _key_task(row).split("/", 1)[0]
        controls.append(
            {
                "item_id": f"control:{key}:{identity}",
                "kind": "task",
                "expected": "clean",
                "task": {
                    "source_id": source,
                    "task_id": bare_task(_key_task(row)),
                    "upstream_revision": package.get("pin"),
                    "task_version_digest": subject["task_digest"],
                },
                "package_path": str(Path(package["root_hint"]).expanduser() / package["name"])
                if package.get("root_hint") and package.get("name")
                else None,
                "key_id": key,
                "key_item_id": identity,
                "label_source": row["label_source"],
                "label_strength": row.get("label_strength", "source_negative"),
                "record_sha256": digest(row),
                "finding_ids": [],
            }
        )
    return controls


def original_controls(root: Path, state_root: Path) -> list[dict[str, Any]]:
    """Use the source owner's original-row loader, never invent clean versions."""
    if not (root / "research/failure-library/findings/impossiblebench.jsonl").is_file():
        return []
    source = import_module("evallab.failure_library.sources.constructed")

    if not all(
        (
            state_root
            / "raw/impossiblebench"
            / pin["revision"]
            / "data/original-00000-of-00001.parquet"
        ).is_file()
        for pin in source.IMPOSSIBLE_PINS.values()
    ):
        return []

    return source.impossible_controls(root, state_root=state_root)


def _original_rows(records: list[dict[str, Any]], denied: set[str]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        task = record["task"]
        task = task.model_dump(mode="json") if hasattr(task, "model_dump") else dict(task)
        family = task["task_id"].rsplit("/", 1)[0]
        if record["split"] != "DEV" or _non_dev_task(task, denied):
            continue
        payload = {**record, "task": task}
        rows.append(
            {
                "item_id": f"control:{task['source_id']}:{record['locator']}",
                "kind": "task",
                "expected": "clean",
                "task": task,
                "control_kind": "impossible_original",
                "family": family,
                "label_source": record["label_source"],
                "label_strength": "construction_negative",
                "raw_row_digest": record["raw_row_digest"],
                "artifact_sha256": record["artifact_sha256"],
                "record_sha256": digest(payload),
                "finding_ids": [],
                "package_path": None,
            }
        )
    return rows


def _control_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    task = row["task"]
    return (
        task["source_id"],
        task["task_id"],
        task.get("task_version_digest") or row["raw_row_digest"],
    )


def run_label(run: RunRecord) -> str | None:
    """Reward or a judge suspicion alone is never a hacked/clean label."""
    labels = {label.label.casefold() for label in run.labels if label.kind in {"executed", "human"}}
    positive, negative = bool(labels & HACK_LABELS), bool(labels & CLEAN_LABELS)
    return "hacked" if positive and not negative else "clean" if negative and not positive else None


def select_canary(
    root: Path,
    store: LibraryStore,
    *,
    tasks: int = 20,
    runs: int = 40,
    originals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if tasks < 1 or runs < 2 or runs % 2:
        raise ValueError("tasks must be positive and runs must be positive and even")
    assignments, denied = split_index(root)
    keys = _key_rows(root)
    findings = list(store.iter_findings())
    controls = _controls(keys, assignments, denied) + _original_rows(originals or [], denied)
    control_sources = {row["task"]["source_id"] for row in controls}
    by_task: dict[tuple[str, str, str | None, str | None], list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.split == "DEV" and not _non_dev_task(finding.task.model_dump(), denied):
            by_task[_task_identity(finding.task.model_dump())].append(finding)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for identity, group in by_task.items():
        ordered = sorted(group, key=lambda f: (TIER_ORDER.index(f.tier), f.finding_id))
        first = ordered[0]
        payload = [f.model_dump(mode="json") for f in sorted(group, key=lambda f: f.finding_id)]
        buckets[first.tier].append(
            {
                "item_id": "task:" + digest(identity).removeprefix("sha256:")[:20],
                "kind": "task",
                "expected": "broken",
                "task": first.task.model_dump(),
                "tier": first.tier,
                "finding_ids": [row["finding_id"] for row in payload],
                "collections": sorted({finding.collection for finding in group}),
                "record_sha256": digest(payload),
                "label_source": first.source_url,
                "label_strength": first.tier,
                **_binding(first, keys),
            }
        )
    for bucket in buckets.values():
        bucket.sort(
            key=lambda row: (row["task"]["source_id"] not in control_sources, _rank(row["item_id"]))
        )
    selected = []
    while len(selected) < tasks and any(buckets.values()):
        for tier in TIER_ORDER:
            if buckets[tier] and len(selected) < tasks:
                selected.append(buckets[tier].pop(0))
    used_controls: set[tuple[str, str, str]] = set()
    broken_digests = {(bare_task(f.task.task_id), f.task.task_version_digest) for f in findings}
    matched = []
    for broken in selected:
        candidates = [
            row
            for row in controls
            if _control_identity(row) not in used_controls
            and (bare_task(row["task"]["task_id"]), row["task"].get("task_version_digest"))
            not in broken_digests
            and row["task"]["source_id"] == broken["task"]["source_id"]
            and (
                not row.get("family")
                or row["family"] == broken["task"]["task_id"].rsplit("/", 1)[0]
            )
        ]
        candidates.sort(
            key=lambda row: (
                row["task"].get("upstream_revision") != broken["task"].get("upstream_revision"),
                _rank(broken["item_id"] + row["item_id"]),
            )
        )
        if candidates:
            control: dict[str, Any] = {
                **candidates[0],
                "task": {
                    **candidates[0]["task"],
                    "upstream_repo": candidates[0]["task"].get("upstream_repo")
                    or broken["task"]["upstream_repo"],
                },
                "matched_to": broken["item_id"],
                "match_basis": "same_original_problem"
                if candidates[0].get("family")
                else "same_source; prefer_same_revision",
            }
            matched.append(control)
            used_controls.add(_control_identity(control))
    run_buckets: dict[str, list[dict[str, Any]]] = {"hacked": [], "clean": []}
    verified_collections: set[str] = set()
    for run in store.iter_runs():
        label = run_label(run)
        if run.split != "DEV" or bare_task(run.task.task_id) in denied or label is None:
            continue
        if run.collection not in verified_collections:
            if problems := store.verify_runs(run.collection):
                raise ValueError("; ".join(problems))
            verified_collections.add(run.collection)
        run_buckets[label].append(
            {
                "item_id": f"run:{run.collection}:{run.run_id}",
                "kind": "run",
                "expected": label,
                "run_id": run.run_id,
                "collection": run.collection,
                "task": run.task.model_dump(),
                "transcript_ref": run.transcript_ref,
                "model": run.model,
                "reward": run.reward,
                "finding_ids": run.finding_ids,
                "record_sha256": digest(run.model_dump(mode="json")),
                "label_source": [
                    label.model_dump()
                    for label in run.labels
                    if label.kind in {"executed", "human"}
                ],
            }
        )
    selected_runs = [
        row
        for label in ("hacked", "clean")
        for row in sorted(run_buckets[label], key=lambda row: _rank(row["item_id"]))[: runs // 2]
    ]
    sources = {}
    for path in sorted(store.findings_root.glob("*.jsonl")):
        sources[str(path.relative_to(root))] = (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    for collection in sorted(verified_collections):
        path = store.manifest_path(collection)
        sources[str(path.relative_to(root))] = (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    selected_key_ids = {row["key_id"] for row in selected + matched if row.get("key_id")}
    for key_id in sorted(selected_key_ids):
        path = root / "research/answer-keys" / key_id / "items.jsonl"
        sources[str(path.relative_to(root))] = (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    assignment_path = root / "research/answer-keys/splits/assignments.csv"
    sources["research/answer-keys/splits/assignments.csv"] = (
        "sha256:" + hashlib.sha256(assignment_path.read_bytes()).hexdigest()
    )
    body = {
        "schema": SCHEMA,
        "seed": SEED,
        "split": "DEV",
        "requested": {
            "broken_tasks": tasks,
            "clean_tasks": tasks,
            "hacked_runs": runs // 2,
            "clean_runs": runs // 2,
        },
        "selection_rule": "SHA256(seed,NUL,id), task round-robin strongest evidence tiers; unique task version; matched controls without known findings; run labels executed/human only; veto any cross-key non-DEV bare task",
        "counts": {
            "broken_tasks": len(selected),
            "clean_tasks": len(matched),
            "hacked_runs": sum(row["expected"] == "hacked" for row in selected_runs),
            "clean_runs": sum(row["expected"] == "clean" for row in selected_runs),
        },
        "task_tiers": dict(sorted(Counter(row["tier"] for row in selected).items())),
        "source_digests": sources,
        "tasks": selected + matched,
        "runs": selected_runs,
        "control_caveat": "Clean means the cited source negative, not a certificate of task correctness; weak negatives retain their strength. Missing controls are a deficit, never synthesized.",
    }
    body["slice_sha256"] = digest(body)
    return body


def load_frozen(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    claimed = value.pop("slice_sha256", None)
    if value.get("schema") != SCHEMA or claimed != digest(value) or value.get("split") != "DEV":
        raise ValueError("canary manifest schema, DEV split or digest is invalid")
    value["slice_sha256"] = claimed
    return value


def freeze(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        return load_frozen(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def verify_snapshot(
    root: Path,
    store: LibraryStore,
    manifest: dict[str, Any],
    *,
    originals: list[dict[str, Any]] | None = None,
) -> None:
    """Refuse label/version/split drift before running either arm on frozen IDs."""
    assignments, denied = split_index(root)
    findings = {item.finding_id: item for item in store.iter_findings()}
    keys = {(key, str(row.get("item_id"))): row for key, row in _key_rows(root)}
    original_rows = {row["item_id"]: row for row in _original_rows(originals or [], denied)}
    runs: dict[tuple[str, str], RunRecord] = {}
    verified: set[str] = set()
    for item in manifest["runs"]:
        collection = item["collection"]
        if collection not in verified:
            if problems := store.verify_runs(collection):
                raise ValueError("; ".join(problems))
            runs.update(((collection, run.run_id), run) for run in store.iter_runs(collection))
            verified.add(collection)
    for item in manifest["tasks"] + manifest["runs"]:
        if _non_dev_task(item["task"], denied):
            raise ValueError(f"frozen canary is now cross-key non-DEV: {item['item_id']}")
        if item["kind"] == "run":
            run = runs.get((item["collection"], item["run_id"]))
            if (
                run is None
                or run.split != "DEV"
                or run_label(run) != item["expected"]
                or digest(run.model_dump(mode="json")) != item["record_sha256"]
            ):
                raise ValueError(f"frozen run changed or disappeared: {item['item_id']}")
        elif item["expected"] == "broken":
            retained = [findings.get(identity) for identity in item["finding_ids"]]
            if any(finding is None or finding.split != "DEV" for finding in retained):
                raise ValueError(f"frozen finding changed or disappeared: {item['item_id']}")
            payload = [
                finding.model_dump(mode="json") for finding in retained if finding is not None
            ]
            if digest(payload) != item["record_sha256"]:
                raise ValueError(f"frozen finding bytes changed: {item['item_id']}")
        elif item.get("control_kind") == "impossible_original":
            row = original_rows.get(item["item_id"])
            if row is None or row["record_sha256"] != item["record_sha256"]:
                raise ValueError(
                    f"frozen original control changed or disappeared: {item['item_id']}"
                )
        else:
            row = keys.get((item["key_id"], item["key_item_id"]))
            if (
                row is None
                or digest(row) != item["record_sha256"]
                or assignments.get((item["key_id"], item["key_item_id"])) != "dev"
            ):
                raise ValueError(f"frozen control changed or disappeared: {item['item_id']}")
