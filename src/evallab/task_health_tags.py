"""Harbor metadata projections of the Python task ledger (HAR-172).

These are historical evidence labels, not task admission or new grades. Only
``task.toml`` metadata changes; task-variant records retain the original package
binding. Copied and infrastructure outcomes are unknown, not solve failures.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.storage.paths import shared_checkout_root
from evallab.task_health import load_pool
from evallab.task_variants import (
    RECORDS_DIRNAME,
    VariantInvalid,
    VariantRecord,
    default_variants_root,
    derive_task,
    load_records,
    materialize,
    verify,
)

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
POOL = Path("research/experiments/har108-python-census/pool.json")
TRANSFORM = "task-health-tags@1"
SCHEMA = "evallab.task_health_tags/v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
_OUTCOMES = ("clean_pass", "copied_pass", "fail", "infra")
_PROBE_VERDICTS = {"cracked", "leak-found-not-cracked", "clean", "unscored"}


def _path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _source(path: Path) -> tuple[bytes, dict[str, str]]:
    raw = path.read_bytes()
    return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _csv_rows(raw: bytes, *, required: set[str], label: str) -> dict[str, dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    if not required <= set(reader.fieldnames or []):
        raise ValueError(
            f"{label}: missing columns {sorted(required - set(reader.fieldnames or []))}"
        )
    rows = {}
    for row in reader:
        task_id = row.get("task_id", "")
        if not _TASK_ID.fullmatch(task_id) or task_id in rows:
            raise ValueError(f"{label}: invalid or duplicate task_id {task_id!r}")
        if any(row.get(key) is None for key in required):
            raise ValueError(f"{label}: incomplete row for {task_id}")
        rows[task_id] = row
    return rows


def solve_summary(history: Mapping[str, str] | None) -> dict[str, Any]:
    """Classify the #703 history's clean-pass/fail denominator, not raw passes.

    History is task-level, across its recorded packages/models/harnesses. It is
    not a claim of a current-package pass rate or a new counts adjudication.
    """
    if history is None:
        return {"tag": "solve:unknown", "reason": "no history row"}
    counts = {}
    for key in ("runs", *_OUTCOMES):
        value = history.get(key, "")
        if not isinstance(value, str) or not value.isdecimal():
            raise ValueError(f"history: {key} must be a nonnegative integer")
        counts[key] = int(value)
    if counts["runs"] != sum(counts[key] for key in _OUTCOMES):
        raise ValueError("history: runs does not equal the outcome counts")
    known = counts["clean_pass"] + counts["fail"]
    if counts["runs"] == 0:
        tag = "solve:never-run"
    elif known == 0:
        tag = "solve:unscored"
    elif counts["clean_pass"] == 0:
        tag = "solve:0-of-n"
    elif counts["fail"] == 0:
        tag = "solve:always"
    else:
        tag = "solve:mixed"
    return {
        "tag": tag,
        **counts,
        "known_attempts": known,
        "last_run": history.get("last_run", ""),
        "scope": "task history; copied and infra excluded; not current-package capability",
    }


def health_tag(
    ledger: Mapping[str, str],
    locked_nop: Mapping[str, str] | None,
    *,
    exploit_verdict: str | None = None,
) -> str:
    """Adverse bound evidence wins; a validated repair supersedes an old nop.

    ``sound`` means ledger-usable with a sound locked nop, not proof against
    every exploit. Absence of a probe is never translated into a clean verdict.
    """
    status = ledger["status"]
    if status not in {"usable", "review", "discarded", "unchecked"}:
        raise ValueError(f"ledger: unsupported status {status!r}")
    if exploit_verdict not in _PROBE_VERDICTS | {None}:
        raise ValueError(f"exploit: unsupported verdict {exploit_verdict!r}")
    if exploit_verdict == "cracked":
        return "health:cracked"
    # RUN_DEFECTS emits this diagnosis for an image-bound leak. Do not confuse
    # a closed PyPI channel or a merely available package with an observed leak.
    image_leak = ledger.get("reason", "").casefold().startswith("image leaks the fix:")
    open_leak = status == "review" and "with no leak-closed variant" in ledger.get("reason", "")
    if exploit_verdict == "leak-found-not-cracked" or image_leak or open_leak:
        return "health:leak-found"
    if status != "usable":
        return f"health:{status}"
    if ledger.get("run") == "repair" and ledger.get("run_variant_status") == "validated":
        # A HAR-146 row explicitly naming this repair is current adverse
        # evidence; it must not be hidden by the earlier validation status.
        if locked_nop and locked_nop.get("note") == f"repaired:{ledger['run_digest'][7:19]}":
            label = locked_nop.get("locked_nop", "")
            if label in {"broken_environment", "grader_suspect"}:
                return f"health:{label.replace('_', '-')}"
        return "health:repaired"
    if locked_nop is None:
        return "health:unchecked"
    repaired = locked_nop.get("note", "")
    if repaired.startswith("repaired:") and not ledger["run_digest"].removeprefix(
        "sha256:"
    ).startswith(repaired.removeprefix("repaired:")):
        return "health:unchecked"
    label = locked_nop.get("locked_nop", "")
    if label == "sound":
        return "health:sound"
    if label in {"broken_environment", "grader_suspect"}:
        return f"health:{label.replace('_', '-')}"
    return "health:unchecked"


def metadata_bytes(original: bytes, tags: Sequence[str]) -> bytes:
    """Replace only our tag namespaces while preserving other TOML fields."""
    before = tomllib.loads(original.decode("utf-8"))
    document = tomlkit.parse(original.decode("utf-8"))
    if "metadata" not in document:
        document["metadata"] = tomlkit.table()
    metadata = document["metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("task.toml metadata must be a table")
    existing = metadata.get("tags", [])
    if not isinstance(existing, list) or any(not isinstance(tag, str) for tag in existing):
        raise ValueError("task.toml metadata.tags must be a string array")
    combined = list(
        dict.fromkeys(
            [tag for tag in existing if not tag.startswith(("health:", "solve:"))] + list(tags)
        )
    )
    document["metadata"]["tags"] = combined
    output = tomlkit.dumps(document).encode("utf-8")
    after = tomllib.loads(output.decode("utf-8"))
    expected = dict(before)
    expected["metadata"] = {**before.get("metadata", {}), "tags": combined}
    if after != expected:
        raise ValueError("health tags changed non-tag task configuration")
    return output


def _probe_binding(row: Mapping[str, Any], *, input_dir: Path) -> dict[str, str | None]:
    """Bind native HAR-161 output via Lab provenance or its native task lock."""
    digest = row.get("task_package_digest")
    if digest is not None:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("exploit: invalid task_package_digest")
        return {"package": digest, "harbor": None}
    trial_value = row.get("trial")
    if not isinstance(trial_value, str) or not trial_value:
        return {"package": None, "harbor": None}
    trial = _path(input_dir, Path(trial_value))
    metadata_path = trial.parent / "lab-metadata.json"
    if metadata_path.is_file():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        digest = (payload.get("task_staging") or {}).get("source_package_digest")
        if isinstance(digest, str) and _DIGEST.fullmatch(digest):
            return {"package": digest, "harbor": None}
    # Native jobs can lack the Lab sidecar that the published copy retains.
    # A task ref matched to this trial in the immutable lock still identifies
    # the executed Harbor package. Do not guess from a job-name suffix.
    config_path = trial / "config.json"
    if not config_path.is_file():
        return {"package": None, "harbor": None}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    task_path = (config.get("task") or {}).get("path")
    if not isinstance(task_path, str):
        return {"package": None, "harbor": None}
    for lock_path in (trial / "lock.json", trial.parent / "lock.json"):
        if not lock_path.is_file():
            continue
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        refs = [lock] if "task" in lock else lock.get("trials", [])
        digests = {
            ref["task"]["digest"]
            for ref in refs
            if isinstance(ref, dict)
            and isinstance(ref.get("task"), dict)
            and ref["task"].get("path") == task_path
            and isinstance(ref["task"].get("digest"), str)
            and _DIGEST.fullmatch(ref["task"]["digest"])
        }
        if len(digests) == 1:
            return {"package": None, "harbor": next(iter(digests))}
    return {"package": None, "harbor": None}


def _probe_rows(raw: bytes, *, input_dir: Path) -> dict[str, dict[str, Any]]:
    """Read ``probe-exploit verdict``'s task-id-keyed JSON, not HAR-83 labels."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("exploit verdicts must be a task-id-keyed JSON object")
    rows = {}
    for task_id, row in payload.items():
        if not _TASK_ID.fullmatch(task_id) or not isinstance(row, dict):
            raise ValueError(f"exploit: invalid task row {task_id!r}")
        if row.get("verdict") not in _PROBE_VERDICTS:
            raise ValueError(f"exploit: unsupported verdict for {task_id}")
        rows[task_id] = {**row, "binding": _probe_binding(row, input_dir=input_dir)}
    return rows


def _view_link(view_root: Path, task_id: str, package: Path, variants_root: Path) -> None:
    """Publish a flat, native-viewer task collection over immutable variants."""
    view_root.mkdir(parents=True, exist_ok=True)
    link = view_root / task_id
    if link.is_symlink():
        if link.resolve() == package.resolve():
            return
        if not link.resolve().is_relative_to(variants_root.resolve()):
            raise ValueError(f"refusing to replace an unrelated task-view link: {link}")
        link.unlink()
    elif link.exists():
        raise ValueError(f"refusing to replace an existing task-view path: {link}")
    link.symlink_to(package.resolve(), target_is_directory=True)


def generate_health_tags(
    repo_root: Path,
    *,
    ledger_path: Path = LEDGER,
    locked_nop_path: Path = LOCKED_NOP,
    history_path: Path = HISTORY,
    pool_path: Path = POOL,
    exploit_path: Path | None = None,
    source_root: Path | None = None,
    records_dir: Path = RECORDS_DIRNAME,
    variants_root: Path | None = None,
    view_root: Path | None = None,
    task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Generate metadata variants for every ledger row (or an explicit subset).

    This never downloads, runs, admits, or edits a parent package or a trial.
    Missing packages/digest drift fail rather than silently omitting a task.
    """
    root = repo_root.resolve()
    source = source_root.resolve() if source_root is not None else shared_checkout_root(root)
    store = _path(root, variants_root) if variants_root is not None else default_variants_root(root)
    view = _path(root, view_root) if view_root is not None else None
    sources: dict[str, Any] = {}
    data = {}
    for name, path, columns in (
        (
            "ledger",
            ledger_path,
            {"task_id", "status", "reason", "run", "run_digest", "run_variant_status"},
        ),
        ("locked_nop", locked_nop_path, {"task_id", "locked_nop", "note"}),
        ("history", history_path, {"task_id", "runs", *_OUTCOMES}),
    ):
        raw, sources[name] = _source(_path(root, path))
        data[name] = _csv_rows(raw, required=columns, label=name)
    pool_file = _path(root, pool_path)
    _, sources["pool"] = _source(pool_file)
    pool = {entry["task_id"]: entry for entry in load_pool(pool_file)}
    probes: dict[str, dict[str, Any]] = {}
    if exploit_path is not None:
        probe_file = _path(root, exploit_path)
        raw, sources["exploit"] = _source(probe_file)
        probes = _probe_rows(raw, input_dir=root)
    else:
        sources["exploit"] = {"status": "not supplied; not a clean verdict"}
    ledger = data["ledger"]
    selected = sorted(set(task_ids)) if task_ids is not None else sorted(ledger)
    missing = set(selected) - ledger.keys()
    if missing:
        raise ValueError(f"tasks absent from ledger: {sorted(missing)}")
    # Parent records remain in the canonical root even when output records are
    # isolated (e.g. smoke checks). Neither record set is overwritten.
    parents = {record.variant_digest: record for record in load_records(root)}
    existing = load_records(root, records_dir=records_dir)
    by_parent: dict[tuple[str, str], list[VariantRecord]] = {}
    for record in existing:
        if record.transform == TRANSFORM:
            by_parent.setdefault((record.task_name, record.parent.digest), []).append(record)
    results = []
    for task_id in selected:
        row = ledger[task_id]
        digest = row["run_digest"]
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"{task_id}: invalid ledger run_digest")
        if row["run"] == "original":
            entry = pool.get(task_id)
            if entry is None or entry["task_version_digest"] != digest:
                raise ValueError(f"{task_id}: pool does not bind the ledger package")
            parent = _path(source, Path(entry["task"]))
            parent_source = {"kind": "local", "path": str(parent)}
        else:
            parent_record = parents.get(digest)
            if parent_record is None or parent_record.task_name.rsplit("/", 1)[-1] != task_id:
                raise ValueError(f"{task_id}: ledger variant record is missing or mismatched")
            if row["run_variant_status"] != parent_record.status:
                raise ValueError(f"{task_id}: ledger and variant status disagree")
            parent = (
                source
                / "derived/task-store/variants"
                / parent_record.task_slug
                / parent_record.digest12
            )
            parent_source = {"kind": "variant", "record": parent_record.record_relpath().as_posix()}
        if not parent.is_dir() or task_directory_digest(parent) != digest:
            raise ValueError(f"{task_id}: parent package missing or differs from ledger: {parent}")
        probe = probes.get(task_id)
        bound_probe = False
        if probe is not None:
            binding = probe["binding"]
            bound_probe = (
                binding["package"] == digest
                if binding["package"] is not None
                else binding["harbor"] == harbor_task_digest(parent)
            )
        health = health_tag(
            row,
            data["locked_nop"].get(task_id),
            exploit_verdict=probe["verdict"] if bound_probe and probe else None,
        )
        solve = solve_summary(data["history"].get(task_id))
        tags = [health, solve["tag"]]
        original = (parent / "task.toml").read_bytes()
        updated = metadata_bytes(original, tags)
        task_name = tomllib.loads(original.decode("utf-8")).get("task", {}).get("name", parent.name)
        assessment = {
            "task_id": task_id,
            "parent_digest": digest,
            "tags": tags,
            "ledger_status": row["status"],
            "ledger_reason": row["reason"],
            "locked_nop": data["locked_nop"].get(task_id),
            "solve": solve,
            "exploit": {
                "verdict": probe["verdict"] if probe else "not-probed",
                "digest_match": bound_probe if probe else None,
                "binding": probe["binding"] if probe else None,
            },
        }
        matches = [
            record
            for record in by_parent.get((task_name, digest), [])
            if len(record.files) == 1
            and record.files[0].path == "task.toml"
            and record.files[0].content == updated.decode("utf-8")
        ]
        if matches:
            record = matches[0]
            package = materialize(
                record, parent, repo_root=root, records_dir=records_dir, variants_root=store
            )
            failures = verify(
                record,
                repo_root=root,
                records_dir=records_dir,
                variants_root=store,
                parent_dir=parent,
            )
            if failures:
                raise VariantInvalid(f"{task_id}: existing health variant invalid: {failures}")
        else:
            record = derive_task(
                parent,
                changes={"task.toml": updated},
                transform=TRANSFORM,
                rationale="Harbor health/solve metadata from recorded task evidence; no admission or new grades",
                created_by="har172-task-health",
                inputs={"sources": sources, "assessment": assessment},
                parent_source=parent_source,
                repo_root=root,
                records_dir=records_dir,
                variants_root=store,
            )
            package = store / record.task_slug / record.digest12
        if view is not None:
            _view_link(view, task_id, package, store)
        results.append(
            {
                **assessment,
                "variant_digest": record.variant_digest,
                "variant_harbor_digest": record.variant_harbor_digest,
                "record": str(
                    _path(root, records_dir) / record.task_slug / f"{record.digest12}.json"
                ),
                "package": str(package),
                "reused": bool(matches),
            }
        )
    return {
        "schema": SCHEMA,
        "sources": sources,
        "tasks": results,
        "view_root": str(view) if view else None,
    }
