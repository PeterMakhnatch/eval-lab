"""Freeze a read-only TB4 provenance census through the existing native boundary.

Run ``--scope primary`` before ``--scope board``. Originals are content-addressed
and never repaired. A failed native command stops further public reads; a rerun
reuses successful bytes and preserves the exact unmeasured remainder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from evallab import hub_trials as hub

INPUT_SCHEMA = "evallab.tb4_provenance_inputs/v1"
LOCK_SCHEMA = "evallab.tb4_package_locks/v1"
HERE = Path(__file__).resolve().parent
DEFAULT_RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-run-census"
DEFAULT_FROZEN = Path.home() / "Developer/eval-lab/.worktrees/infra-tb4-hub-trials-20261020/runs/in2-hub-discovery"


def package_key(source: str, digest: str, task_name: str) -> str:
    return hashlib.sha256(json.dumps([source, digest, task_name], separators=(",", ":")).encode()).hexdigest()


def read_bytes_receipted(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = hub._safe(path).read_bytes()
    return raw, {"path": str(path.absolute()), "sha256": hub._digest(raw), "bytes": len(raw)}


def receipt(path: Path) -> dict[str, Any]:
    return read_bytes_receipted(path)[1]


def read_object(path: Path) -> Any:
    return json.loads(hub._safe(path).read_bytes())


def read_receipted(path: Path) -> tuple[Any, dict[str, Any]]:
    """Bind parsed evidence to exactly the same bytes as its receipt."""
    raw, evidence = read_bytes_receipted(path)
    return json.loads(raw), evidence


def publish(root: Path, name: str, value: Any) -> dict[str, Any]:
    """Replace a derived view only after checking its prior receipt; retain snapshots."""
    raw = hub._json_bytes(value)
    digest = hub._digest(raw)
    snapshot = root / "exports" / f"{name.removesuffix('.json')}-{digest}.json"
    if snapshot.exists():
        if snapshot.read_bytes() != raw:
            raise hub.IntegrityError("Derived snapshot hash mismatch")
    else:
        hub._atomic(snapshot, raw)
    target = root / name
    sidecar = root / f"{name}.receipt.json"
    if target.exists() or sidecar.exists():
        if not target.exists() or not sidecar.exists():
            raise hub.IntegrityError("Untracked derived export; refusing replacement")
        if receipt(target) != read_object(sidecar)["output"]:
            raise hub.IntegrityError("Derived export changed; refusing replacement")
    hub._atomic(target, raw, replace=target.exists())
    result = {"output": receipt(target), "immutable_snapshot": receipt(snapshot)}
    hub._atomic(sidecar, hub._json_bytes(result), replace=sidecar.exists())
    return result


class NativeRefusal(hub.HubError):
    """The native read boundary failed; do not issue further uncached reads."""


class FrozenNative(hub.NativeHarbor):
    """Feed explicitly identified prior native stdout into the unchanged cache."""

    def __init__(self, executable: str, sources: dict[tuple[str, ...], Path], offline: bool, refusal_path: Path | None = None):
        super().__init__(executable)
        self.sources = sources
        self.offline = offline
        self.refusal_path = refusal_path
        self.refused = bool(refusal_path and refusal_path.exists())
        self.refusal_lock = threading.Lock()

    def json(self, args: Any) -> bytes:
        source = self.sources.get(tuple(args))
        if source is not None:
            return hub._safe(source).read_bytes()
        if self.refused:
            raise NativeRefusal("not_attempted_after_native_refusal")
        if self.offline:
            raise NativeRefusal("Native read unavailable in offline mode")
        try:
            return super().json(args)
        except hub.HubError as exc:
            with self.refusal_lock:
                self.refused = True
                if self.refusal_path is not None and not self.refusal_path.exists():
                    hub._atomic(self.refusal_path, hub._json_bytes({"command": list(args), "error": str(exc), "policy": "no automatic retry; preserve missing coverage"}))
            raise NativeRefusal(str(exc)) from exc


def frozen_sources(root: Path) -> dict[tuple[str, ...], Path]:
    sources = {
        ("hub", "leaderboard", "show", hub.BOARD): root / "tb4-leaderboard.stdout",
        ("version", "show", f"{hub.PACKAGE}@{hub.DATASET_DIGEST}", "--tasks"): root / "tb4-dataset-digest.stdout",
        ("version", "show", f"{hub.PACKAGE}@4"): root / "tb4-dataset-revision4.stdout",
        ("version", "show", f"{hub.PACKAGE}@v4.0.0"): root / "tb4-dataset-tag-v400.stdout",
    }
    for row in read_object(root / "board-survey.json"):
        prefix = row["row"][:8]
        sources[("hub", "leaderboard", "row", "trial", "list", row["row"], "--limit", "1000", "--page", "1")] = root / "board-survey" / f"{prefix}-assoc.stdout"
        sources[("hub", "job", "show", row["job_id"])] = root / "board-survey" / f"{prefix}-job-overview.stdout"
        sources[("hub", "trial", "show", row["seed"])] = root / "board-survey" / f"{prefix}-seed-detail.stdout"
    # These are whole, original include-retries pages, not derived lock surveys.
    for prefix in ("opus55", "sonnet-55", "gpt-61-sol", "glm-53", "gemini-38-flash", "grok-47"):
        path = root / f"{prefix}-job-trials-p1.stdout"
        if path.exists():
            payload = read_object(path)
            if payload["items"]:
                job_id = payload["items"][0]["job_id"]
                sources[("hub", "job", "trials", job_id, "--include-retries", "--limit", str(payload["page_size"]), "--page", "1")] = path
    return sources


def cache(root: Path, native: FrozenNative) -> hub.Acquisition:
    root = hub._safe(root)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        state = hub._load(root)
        errors = hub._integrity(root, state)
        if errors:
            raise hub.IntegrityError("; ".join(errors))
    else:
        if any(root.iterdir()):
            raise hub.IntegrityError("Untracked cache directory; use a new target")
        state = {
            "schema": hub.SCHEMA, "request": {"page_size": 1000, "max_pages": 100},
            "metadata": {}, "downloads": {}, "selection": None, "state": "metadata_only",
            "frozen_sources": {},
        }
    # Bind imported bytes to their actual original paths as well as the cache hash.
    for args, source in native.sources.items():
        key = hub._digest(hub._json_bytes(args))
        relative = f"metadata/{key}.json"
        if relative in state["metadata"]:
            original = receipt(source)
            previous = state["frozen_sources"].get(relative)
            if previous is not None and previous != original:
                raise hub.IntegrityError("Frozen native source changed")
            state["frozen_sources"][relative] = original
    result = hub.Acquisition(root, state, native)
    result.save()
    return result


def fetch(acquisition: hub.Acquisition, *args: str) -> tuple[dict, dict]:
    payload = acquisition.fetch(*args)
    relative = f"metadata/{hub._digest(hub._json_bytes(args))}.json"
    source = acquisition.native.sources.get(tuple(args))
    if source is not None:
        original = receipt(source)
        if original["sha256"] != acquisition.manifest["metadata"][relative]["sha256"]:
            raise hub.IntegrityError("Original native stdout differs from cached copy")
        acquisition.manifest["frozen_sources"][relative] = original
        acquisition.save()
    return payload, receipt(acquisition.root / relative)


def fetch_pages(acquisition: hub.Acquisition, command: list[str], id_key: str) -> tuple[list[dict], list[dict]]:
    # Reuse the existing exact pagination/deduplication checks, including page caps.
    items = acquisition.pages(command, id_key)
    receipts = []
    for page in range(1, acquisition.manifest["request"]["max_pages"] + 1):
        args = [*command, "--limit", "1000", "--page", str(page)]
        payload, item_receipt = fetch(acquisition, *args)
        receipts.append(item_receipt)
        if page >= payload["total_pages"]:
            break
    return items, receipts


def freeze_board(acquisition: hub.Acquisition) -> dict[str, Any]:
    board, board_receipt = fetch(acquisition, "hub", "leaderboard", "show", hub.BOARD)
    rows = hub._rows(board, (), ())
    dataset, dataset_receipt = fetch(acquisition, "version", "show", f"{hub.PACKAGE}@{hub.DATASET_DIGEST}", "--tasks")
    if dataset["id"] != hub.DATASET_ID or dataset["content_hash"] != hub.DATASET_DIGEST:
        raise hub.HubError("Frozen TB4 dataset pin mismatch")
    tasks = {}
    for item in dataset["tasks"]:
        version = item["task_version"]
        package = version["package"]
        name = f"{package['org']['name']}/{package['name']}"
        if name in tasks:
            raise hub.HubError("Duplicate canonical task identity")
        tasks[name] = {"task_version_id": item["task_version_id"], "digest": "sha256:" + version["content_hash"].removeprefix("sha256:"), "available": item["available"]}
    membership: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        entries, evidence = fetch_pages(acquisition, ["hub", "leaderboard", "row", "trial", "list", row["id"]], "trial_id")
        if len(entries) != row["n_trials"]:
            raise hub.HubError("Frozen row membership count mismatch")
        row["trial_ids"] = [entry["trial_id"] for entry in entries]
        row["evidence"] = evidence
        for entry in entries:
            membership[entry["trial_id"]].append(row["id"])
    return {
        "board": {"ref": hub.BOARD, "id": hub.BOARD_ID, "raw": board["leaderboard"], "evidence": board_receipt},
        "dataset": {"id": dataset["id"], "digest": dataset["content_hash"], "revision": dataset.get("revision"), "version": dataset.get("version"), "tags": dataset.get("tags"), "evidence": dataset_receipt},
        "rows": rows, "tasks": tasks, "membership": dict(membership),
    }


def census_jobs(acquisition: hub.Acquisition, membership: dict[str, list[str]]) -> tuple[list[dict], dict[str, dict]]:
    jobs, summaries = {}, {}
    remaining = set(membership)
    try:
        while remaining:
            seed = min(remaining)
            detail, seed_receipt = fetch(acquisition, "hub", "trial", "show", seed)
            if detail["id"] != seed:
                raise hub.HubError("Native seed trial identity mismatch")
            job_id = hub._uuid(detail["job_id"])
            if job_id in jobs:
                raise hub.HubError("Associated trial absent from its native job census")
            job, job_receipt = fetch(acquisition, "hub", "job", "show", job_id)
            dataset_refs = [{"name": entry["name"], "ref": entry["ref"]} for entry in job["config"].get("datasets", [])]
            resolved = []
            for ref in dataset_refs:
                if ref["name"] != hub.PACKAGE:
                    continue
                command = ["version", "show", f"{hub.PACKAGE}@{ref['ref']}"]
                if ref["ref"] == hub.DATASET_DIGEST:
                    command.append("--tasks")
                version, version_receipt = fetch(acquisition, *command)
                resolved.append({**ref, "id": version["id"], "content_hash": version["content_hash"], "version": version.get("version"), "evidence": version_receipt})
            entries, evidence = fetch_pages(acquisition, ["hub", "job", "trials", job_id, "--include-retries"], "id")
            if seed not in {entry["id"] for entry in entries}:
                raise hub.HubError("Native seed absent from include-retries job page")
            for entry in entries:
                if entry["job_id"] != job_id or entry["id"] in summaries:
                    raise hub.HubError("Inconsistent native job identity")
                summaries[entry["id"]] = {**entry, "evidence": evidence}
            jobs[job_id] = {
                "id": job_id, "name": job.get("name"), "dataset_refs": dataset_refs,
                "resolved_dataset_refs": resolved,
                "dataset_matches": bool(dataset_refs) and len(resolved) == len(dataset_refs) and all(version["id"] == hub.DATASET_ID and version["content_hash"] == hub.DATASET_DIGEST for version in resolved),
                "all_trial_ids": [entry["id"] for entry in entries],
                "unassociated_trial_ids": [entry["id"] for entry in entries if entry["id"] not in membership],
                "evidence": [job_receipt, seed_receipt, *evidence],
            }
            remaining.difference_update(entry["id"] for entry in entries)
    except NativeRefusal:
        # Membership was already frozen. Unread job/trial metadata remains
        # explicitly absent rather than shrinking the board denominator.
        pass
    return [jobs[key] for key in sorted(jobs)], summaries


def archive_link(results: Path, entry: dict, detail: dict | None) -> dict[str, Any]:
    directory = hub._inside(results, f"trials/{hub._component(entry['task'])}/{hub._component(entry['trial_name'])}")
    evidence, objects, missing = [], {}, []
    for name in ("result.json", "lock.json", "config.json"):
        path = directory / name
        if not path.exists():
            missing.append(name)
            continue
        objects[name], source = read_receipted(path)
        evidence.append(source)
    result = objects.get("result.json", {})
    lock = objects.get("lock.json", {}).get("task", {})
    issues = ["native_detail_missing"] if detail is None else []
    if not result.get("id"):
        issues.append("archive_result_id_missing")
    if result and detail is not None:
        if result.get("task_name") != detail["task_name"]:
            issues.append("archive_task_mismatch")
        if result.get("trial_name") != detail.get("trial_name"):
            issues.append("archive_trial_name_mismatch")
        actual = (detail.get("lock") or {}).get("task") or {}
        if any(lock.get(key) != actual.get(key) for key in ("source", "digest")):
            issues.append("archive_lock_mismatch")
    id_equal = result.get("id") == entry["trial_id"] if result else None
    metadata_differences = ["archive_trial_name_mismatch"] if id_equal and "archive_trial_name_mismatch" in issues else []
    binding_issues = [issue for issue in issues if issue not in metadata_differences]
    return {
        "status": "missing" if missing else ("unresolved" if binding_issues else "inventory_and_native_detail_linked"),
        "link_basis": "exact result.id == Hub UUID plus native task/lock cross-check" if id_equal else "frozen inventory Hub UUID -> exact inventory archive path; native detail task/trial_name/lock cross-check",
        "hub_trial_id": entry["trial_id"], "archive_result_id": result.get("id"),
        "archive_trial_name": result.get("trial_name"), "inventory_trial_name": entry["trial_name"],
        "id_equal": id_equal, "metadata_differences": metadata_differences, "binding_issues": binding_issues,
        "archive_config_task": objects.get("config.json", {}).get("task"),
        "archive_lock_task": lock, "evidence": evidence, "missing": missing, "issues": issues,
    }


def trial_record(trial_id: str, row_ids: list[str], summary: dict, detail: dict | None, detail_evidence: dict | None, tasks: dict, job: dict, error: str | None, archive: dict | None) -> dict[str, Any]:
    issues = []
    if detail is not None:
        for key in ("id", "job_id", "task_name", "status"):
            if detail.get(key) != summary.get(key):
                issues.append(f"native_detail_{key}_mismatch")
        rewards = ((detail.get("verifier_result") or {}).get("rewards") or {})
        if "reward" in rewards and rewards["reward"] != summary.get("reward"):
            issues.append("native_verifier_reward_mismatch")
    task_name = summary.get("task_name")
    native_lock = (detail or {}).get("lock")
    lock = (native_lock or {}).get("task")
    if detail is None:
        lock_status = "not_acquired"
    elif "lock" not in detail:
        lock_status = "missing"
    elif native_lock is None:
        lock_status = "null"
    elif lock is None:
        lock_status = "task_null"
    else:
        lock_status = "present"
    canonical = tasks.get(task_name, {}).get("digest")
    source, digest = (lock or {}).get("source"), (lock or {}).get("digest")
    if detail is None or not source or not digest or canonical is None:
        provenance = "unmeasured"
    elif issues:
        provenance = "identity_mismatch"
    elif source != hub.PACKAGE:
        provenance = "different_source"
    elif digest != canonical:
        provenance = "canonical_source_different_digest"
    else:
        provenance = "canonical_exact_lock"
    status = (detail or {}).get("status", summary.get("status"))
    native_error = summary.get("error_type") or summary.get("hosted_error") or summary.get("hosted_error_code") or (detail or {}).get("exception_info") or (detail or {}).get("hosted_error")
    measured_reported = status == "completed" and summary.get("status") == "completed" and not native_error and summary.get("reward") is not None
    return {
        "hub_trial_id": trial_id, "job_id": summary.get("job_id"), "row_ids": sorted(row_ids),
        "task_name": task_name, "trial_name": (detail or {}).get("trial_name", summary.get("name")),
        "native_status": status, "native_summary_status": summary.get("status"), "native_reward": summary.get("reward"),
        "native_error_type": summary.get("error_type"), "native_has_exception": bool((detail or {}).get("exception_info")),
        "native_has_hosted_error": bool(summary.get("hosted_error") or summary.get("hosted_error_code") or (detail or {}).get("hosted_error")),
        "native_verifier_result": (detail or {}).get("verifier_result"),
        "reported_outcome_measured": measured_reported, "validated_integrity": "not_adjudicated",
        "lock_task": lock, "canonical_digest": canonical, "provenance_status": provenance,
        "native_lock_status": lock_status,
        "exact_source_eligible": provenance == "canonical_exact_lock" and job.get("dataset_matches") is True,
        "job_dataset_matches": job.get("dataset_matches"),
        "package_key": package_key(source, digest, task_name) if source and digest and task_name else None,
        "dataset_refs": job.get("dataset_refs", []), "archive_link": archive,
        "evidence": [*summary.get("evidence", []), *([detail_evidence] if detail_evidence else [])],
        "detail_status": "complete" if detail is not None else ("error" if error else "not_acquired"),
        "acquisition_error": error, "issues": issues,
    }


def package_locks(trials: list[dict]) -> dict[str, Any]:
    packages = {}
    for trial in trials:
        key = trial["package_key"]
        if key is None:
            continue
        entry = packages.setdefault(key, {
            "key": key, "source": trial["lock_task"]["source"], "digest": trial["lock_task"]["digest"],
            "task_name": trial["task_name"], "canonical_digest": trial["canonical_digest"], "dataset_refs": [],
        })
        for ref in trial["dataset_refs"]:
            if ref not in entry["dataset_refs"]:
                entry["dataset_refs"].append(ref)
    for entry in packages.values():
        entry["dataset_refs"].sort(key=lambda ref: (ref["name"], ref["ref"]))
    return {"schema": LOCK_SCHEMA, "packages": [packages[key] for key in sorted(packages)]}


def frozen_primary(results: Path) -> tuple[list[dict], list[dict]]:
    spec_path = HERE / "spec.json"
    spec, spec_receipt = read_receipted(spec_path)
    if spec.get("schema") != "evallab.tb4_provenance_experiment/v1":
        raise hub.HubError("Unsupported frozen experiment specification")
    if (spec["canonical_board"], spec["canonical_dataset_id"], spec["canonical_dataset_digest"]) != (hub.BOARD, hub.DATASET_ID, hub.DATASET_DIGEST):
        raise hub.HubError("Frozen experiment native pins differ")
    labels, evidence = [], [spec_receipt]
    for entry in spec["label_inputs"]:
        path = hub._inside(HERE.parents[2], entry["path"])
        raw, source = read_bytes_receipted(path)
        if source["sha256"] != entry["sha256"] or source["bytes"] != entry["bytes"]:
            raise hub.IntegrityError("Frozen existing-label input changed")
        evidence.append(source)
        if path.suffix == ".jsonl":
            labels.extend(json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip())
    inventory, inventory_receipt = read_receipted(results / "inventory.json")
    evidence.append(inventory_receipt)
    def cells(entries: list[dict]) -> Counter:
        return Counter((entry["row_id"], entry["trial_id"], entry["task"], entry["reward"]) for entry in entries)
    if cells(labels) != cells(inventory) or sorted({entry["task"] for entry in inventory}) != sorted(spec["primary_tasks"]):
        raise hub.HubError("Original primary inventory differs from frozen labelled cells")
    return inventory, evidence


def collect(root: Path, frozen: Path, results: Path, executable: str, scope: str, workers: int, offline: bool = False) -> dict[str, Any]:
    if not 1 <= workers <= 4:
        raise hub.HubError("Native public read concurrency must be between 1 and 4")
    root.mkdir(parents=True, exist_ok=True)
    sources = frozen_sources(frozen)
    native = FrozenNative(executable, sources, offline, root / "native-refusal.json")
    acquisition = cache(root / "cache", native)
    frozen_board = freeze_board(acquisition)
    membership = frozen_board["membership"]
    inventory, primary_evidence = frozen_primary(results)
    existing_rows, rows_receipt = read_receipted(results / "rows.json")
    if {row["row_id"] for row in existing_rows} != {row["id"] for row in frozen_board["rows"]}:
        raise hub.HubError("Label inventory and frozen native board row UUIDs differ")
    for row in existing_rows:
        if set(row["trial_ids"]) != {trial for trial, rows in membership.items() if row["row_id"] in rows}:
            raise hub.HubError("Label census row memberships differ from frozen native board")
    ids = [entry["trial_id"] for entry in inventory]
    if len(ids) != len(set(ids)):
        raise hub.HubError("Duplicate primary inventory Hub UUID")
    primary = {entry["trial_id"]: entry for entry in inventory}
    for entry in inventory:
        if entry["row_id"] not in membership.get(entry["trial_id"], []):
            raise hub.HubError("Primary trial is not a frozen native row member")
    jobs, summaries = census_jobs(acquisition, membership)
    jobs_by_id = {job["id"]: job for job in jobs}
    requested = sorted(primary if scope == "primary" else membership)
    shards: dict[str, hub.Acquisition] = {}
    locks = {f"{number:02x}": threading.Lock() for number in range(256)}
    details, detail_receipts, errors = {}, {}, {}
    failure_path = root / "native-refusal.json"
    stopped = threading.Event()
    if failure_path.exists():
        stopped.set()
    shard_guard = threading.Lock()

    def acquire_one(identity: str) -> tuple[str, dict | None, dict | None, str | None]:
        shard = identity[:2]
        with locks[shard]:
            with shard_guard:
                if shard not in shards:
                    shards[shard] = cache(root / "details" / shard, native)
                local = shards[shard]
            args = ("hub", "trial", "show", identity)
            relative = f"metadata/{hub._digest(hub._json_bytes(args))}.json"
            is_cached = relative in local.manifest["metadata"] or args in sources
            if stopped.is_set() and not is_cached:
                return identity, None, None, "not_attempted_after_native_refusal"
            try:
                detail, evidence = fetch(local, *args)
                return identity, detail, evidence, None
            except hub.IntegrityError:
                raise
            except hub.HubError as exc:
                stopped.set()
                return identity, None, None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(requested)
        pending = {pool.submit(acquire_one, identity) for identity in [next(iterator, None) for _ in range(workers)] if identity is not None}
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                identity, detail, evidence, error = future.result()
                if detail is not None:
                    details[identity], detail_receipts[identity] = detail, evidence
                else:
                    errors[identity] = error
                    if error != "not_attempted_after_native_refusal" and not failure_path.exists():
                        hub._atomic(failure_path, hub._json_bytes({"hub_trial_id": identity, "error": error, "policy": "no automatic retry; remaining exact coverage stays unmeasured"}))
                identity = next(iterator, None)
                if identity is not None:
                    pending.add(pool.submit(acquire_one, identity))
    trials = []
    for identity in sorted(membership):
        summary = summaries.get(identity, {"id": identity, "job_id": None, "task_name": None, "status": None, "reward": None})
        detail = details.get(identity)
        archive = archive_link(results, primary[identity], detail) if identity in primary else None
        record = trial_record(identity, membership[identity], summary, detail, detail_receipts.get(identity), frozen_board["tasks"], jobs_by_id.get(summary["job_id"], {}), errors.get(identity), archive)
        record["primary_labelled"] = identity in primary
        if identity in primary:
            original = primary[identity]
            record["primary_inventory"] = original
            if summary.get("task_name") != f"terminal-bench/{original['task']}" or summary.get("job_id") != original["job_id"]:
                record["issues"].append("primary_inventory_identity_mismatch")
            if summary.get("reward") != original["reward"]:
                record["issues"].append("primary_inventory_reward_mismatch")
            if record["issues"]:
                record["provenance_status"] = "identity_mismatch"
        trials.append(record)
    coverage = {
        "scope": scope, "rows": len(frozen_board["rows"]), "row_trial_memberships": sum(len(row["trial_ids"]) for row in frozen_board["rows"]),
        "associated_unique_trials": len(trials), "primary_inventory_trials": len(inventory), "requested_details": len(requested),
        "details_acquired": len(details), "detail_errors": errors,
        "job_summaries_acquired": len(jobs), "missing_summary_ids": [identity for identity in sorted(membership) if identity not in summaries],
        "detail_statuses": dict(Counter(trial["detail_status"] for trial in trials)),
        "provenance_statuses": dict(Counter(trial["provenance_status"] for trial in trials)),
        "native_statuses": dict(Counter(trial["native_status"] or "unavailable" for trial in trials)),
        "native_lock_statuses": dict(Counter(trial["native_lock_status"] for trial in trials)),
        "missing_or_incomplete_task_lock_ids": [trial["hub_trial_id"] for trial in trials if not (trial["lock_task"] or {}).get("source") or not (trial["lock_task"] or {}).get("digest")],
        "unassociated_job_trials_excluded": sum(len(job["unassociated_trial_ids"]) for job in jobs),
        "native_refusal": read_object(failure_path) if failure_path.exists() else None,
        "missing_detail_ids": [trial["hub_trial_id"] for trial in trials if trial["detail_status"] != "complete"],
    }
    manifest = {
        "schema": INPUT_SCHEMA, "scope": scope, "board": frozen_board["board"], "dataset": frozen_board["dataset"],
        "tasks": frozen_board["tasks"], "rows": frozen_board["rows"], "jobs": jobs, "trials": trials, "coverage": coverage,
        "source_inputs": [*primary_evidence, rows_receipt, receipt(frozen / "board-survey.json")],
        "scientific_boundary": "Reported native outcomes only; source mismatch is not a task defect; equal verifier bytes do not establish runtime equivalence; existing labels are not changed.",
    }
    output = publish(root, "input-manifest.json", manifest)
    lock_output = publish(root, "package-locks.json", package_locks(trials))
    publish(root, f"{scope}-coverage.json", coverage)
    return {"coverage": coverage, "manifest": output, "package_locks": lock_output}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, default=DEFAULT_FROZEN)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--harbor", required=True)
    parser.add_argument("--scope", choices=("primary", "board"), required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        with hub._locked(args.output):
            result = collect(args.output, args.frozen, args.results, args.harbor, args.scope, args.workers, args.offline)
    except (hub.HubError, OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo arbitrary exceptions, native stderr, response bodies or URLs.
        print(json.dumps({"error": str(exc) if isinstance(exc, hub.HubError) else type(exc).__name__}))
        return 2
    coverage = result["coverage"]
    print(json.dumps({"scope": args.scope, "rows": coverage["rows"], "associated_unique_trials": coverage["associated_unique_trials"], "details_acquired": coverage["details_acquired"], "missing_details": len(coverage["missing_detail_ids"]), "provenance_statuses": coverage["provenance_statuses"], "manifest": result["manifest"], "package_locks": result["package_locks"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
