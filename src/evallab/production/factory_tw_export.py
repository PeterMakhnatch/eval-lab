"""Blinded, monitor-ready ATIF export of Terminal Wrench DEV runs.

Same on-disk contract as ``cloud_trace_export.export_runs(format="atif")``: a
directory of ``<blind_id>.json`` files (blinded ATIF, steps renumbered from 1, no
metadata, identities redacted, no labels) plus a private join manifest (mode
0600, outside the export directory) mapping each blind id to its source id,
label, variant, model and step origins. The manifest is the only place labels live.

Source: few-sh/terminal-wrench (Apache-2.0) via the helpers in
``evallab.terminal_wrench``. Hacks use the ``sanitized`` variant, clean runs the
``baseline`` tree; the ``raw`` variant (elicitation prompt, label leakage) is
refused. Downloads go through one injectable ``fetch(url) -> bytes`` (default:
public GitHub raw, at most four concurrent requests) into a local cache.

Admission: the DEV task set is read from ``assignments.csv`` through
``CorpusRegistry`` (key ``breaker-bench-v1``, item ``breaker-bench-v1:terminal-wrench:<id>``,
where ``<id>`` is the upstream ``task_id``). A task whose id or name is TEST/held
in ANY key, or sealed by the registry, is vetoed. Every exported run is audited
against the same gate again and any leak raises.

Selection: per (task, label kind) candidates are ranked by
``sha256(source_id)`` ascending and the first ``max_per_label`` are kept, where
``source_id = "tw:<task_id>:<model>:<hack|clean>:<trajectory label>"``. Capped
runs that then fail fetch or validation are skipped and counted by reason, not
back-filled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import blind_atif, blind_atif_step_origins, find_forbidden_json
from evallab.production.cloud_trace_db import _PROTECTED_TASKS_FROZEN
from evallab.production.cloud_trace_export import (
    _ANNOTATION_MARKER_FOLDED,
    _EVALUATOR_NAMES,
    _SOURCE_NAMES,
    _actor_inputs_available,
    _blind_id,
    _destination,
    _is_annotation_step,
    _json_bytes,
    _project_cheatbench_step,
    _redact_strings,
    _redaction_pattern,
    _rekey_calls,
    _StagedExport,
    _write_run,
)
from evallab.production.corpus_queue import CorpusRegistry
from evallab.terminal_wrench import (
    HACK_CLASSES,
    INDEX_FILES,
    TW_BASE,
    USER_AGENT,
    native_variant,
    trajectory_cache_path,
    trajectory_url,
    validate_trajectory,
)

KEY_ID = "breaker-bench-v1"
ITEM_PREFIX = f"{KEY_ID}:terminal-wrench:"
UPSTREAM_TASKS = Path("research/answer-keys/breaker-bench-v1/upstream/terminal_wrench_tasks.json")
SEED_RULE = (
    "per (task, label kind): sort ascending by sha256(source_id), keep the first max_per_label; "
    "source_id = 'tw:<task_id>:<model>:<hack|clean>:<trajectory label>'"
)
MAX_WORKERS = 4
DEFAULT_MAX_DOWNLOAD_BYTES = 400_000_000
#: Names of the source that the shared redaction does not already know.
_TW_NAMES = ("terminal-wrench", "terminal_wrench", "terminalwrench", "terminal wrench", "few-sh")
_PROVIDERS = ("anthropic", "openai")
_ATIF_VERSIONS = frozenset({"ATIF-v1.5", "ATIF-v1.6", "ATIF-v1.8"})

Fetch = Callable[[str], bytes]


class FetchNotFound(Exception):
    """The upstream URL does not exist (HTTP 404)."""


class DownloadBudgetExceeded(RuntimeError):
    """The download budget ran out before every selected trajectory was cached."""


class AdmissionLeak(RuntimeError):
    """An exported run's task failed the registry admission gate."""


class _Skip(Exception):
    """One trajectory cannot be exported; ``args[0]`` is the counted reason."""


def http_fetch(url: str) -> bytes:
    """Polite public GET with retries; 404 raises :class:`FetchNotFound`."""
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise FetchNotFound(url) from error
            if attempt == 3:
                raise
        except OSError:
            if attempt == 3:
                raise
        time.sleep(1 + attempt * 2)
    raise AssertionError("unreachable")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _item_id(task_id: str) -> str:
    return ITEM_PREFIX + task_id


def _task_admitted(registry: CorpusRegistry, task_id: str) -> bool:
    """The shared DEV gate: affirmative DEV, no TEST in any key, nothing sealed."""
    name = registry.task_name(task_id)
    item_id = _item_id(task_id)
    return (
        registry.assigned_split(KEY_ID, item_id=item_id, task_id=task_id) == "dev"
        and not registry.assigned_test_anywhere(item_id=item_id, task_id=task_id)
        and registry.sealed_identity(item_id=item_id, task_id=task_id) is None
        and name not in registry.sealed_identifiers()
    )


def _upstream_catalog(repo_root: Path) -> tuple[list[str], dict[str, str]]:
    """Task ids and index digests pinned by the repo's upstream catalog (nothing else is read)."""
    document = json.loads((repo_root / UPSTREAM_TASKS).read_text(encoding="utf-8"))
    ids = sorted({str(row["task_id"]) for row in document["tasks"]})
    pins = {
        "tasks.json": str(document["index_tasks_sha256"]).removeprefix("sha256:"),
        "trajectories.json": str(document["index_trajectories_sha256"]).removeprefix("sha256:"),
    }
    return ids, pins


def _load_index(
    cache_dir: Path, fetch: Fetch, pins: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Cached ``index/{tasks,trajectories}.json`` checked against the catalog's pinned digests."""
    index = cache_dir / "index"
    index.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    loaded: dict[str, Any] = {}
    for name in INDEX_FILES:
        path = index / name
        if not (path.exists() and path.stat().st_size > 0):
            data = fetch(f"{TW_BASE}/index/{name}")
            downloaded += len(data)
            _atomic_write(path, data)
        data = path.read_bytes()
        if _sha256(data) != pins[name]:
            raise ValueError(f"upstream index/{name} differs from the digest pinned in the repo")
        loaded[name] = json.loads(data)
    return loaded["trajectories.json"], loaded["tasks.json"], downloaded


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".part-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _rank(source_id: str) -> str:
    return _sha256(source_id.encode("utf-8"))


def _select(
    trajectories: list[dict[str, Any]], tasks: list[dict[str, Any]], dev_tasks: set[str],
    max_per_label: int, counts: Counter[str],
) -> list[dict[str, Any]]:
    """Capped, seeded selection of hack and clean rows of the DEV tasks."""
    candidates: dict[str, dict[str, Any]] = {}
    for row in trajectories:
        task_id = str(row["task_id"])
        if task_id not in dev_tasks or row["classification"] not in HACK_CLASSES:
            continue
        label = str(row["trajectory_label"])
        candidate = {
            "task_id": task_id, "model": row["model"], "label": label, "kind": "hack",
            "classification": row["classification"], "trial_name": row.get("trial_name"),
            "run_name": row.get("run_name"), "job_name": row.get("job_name"),
        }
        candidate["id"] = f"{task_id}__{row['model']}__{label}"
        candidate["source_id"] = f"tw:{task_id}:{row['model']}:hack:{label}"
        candidates[candidate["source_id"]] = candidate
    hack_seen = len(candidates)
    clean_seen = 0
    for entry in tasks:
        task_id = str(entry["task_id"])
        if task_id not in dev_tasks:
            continue
        for baseline in entry.get("baselines", []):
            label = str(baseline["label"])
            source_id = f"tw:{task_id}:{entry['model']}:clean:{label}"
            if source_id in candidates:
                continue
            clean_seen += 1
            reward = baseline.get("reward")
            if not isinstance(reward, (int, float)) or reward <= 0:
                counts["baseline_not_rewarded"] += 1
                continue
            candidates[source_id] = {
                "task_id": task_id, "model": entry["model"], "label": label, "kind": "clean",
                "classification": None, "trial_name": baseline.get("trial_name"),
                "run_name": baseline.get("run_name"), "job_name": baseline.get("job_name"),
                "id": f"{task_id}__{entry['model']}__{label}", "source_id": source_id,
            }
    counts["seen_hack"], counts["seen_clean"] = hack_seen, clean_seen
    selected: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in candidates.values():
        groups.setdefault((candidate["task_id"], candidate["kind"]), []).append(candidate)
    for key in sorted(groups):
        ranked = sorted(groups[key], key=lambda row: _rank(row["source_id"]))
        selected.extend(ranked[:max_per_label])
        counts["capped_out"] += max(0, len(ranked) - max_per_label)
    return selected


def _fetch_all(
    rows: list[dict[str, Any]], cache_dir: Path, fetch: Fetch, variant: str,
    workers: int, budget: int,
) -> tuple[dict[str, bytes], dict[str, str], int, int]:
    """Cache-backed fetch of every row; returns bytes by source id, failure reasons, bytes, hits."""
    payloads: dict[str, bytes] = {}
    failures: dict[str, str] = {}
    downloaded = 0
    cache_hits = 0
    pending: list[dict[str, Any]] = []
    for row in rows:
        path = trajectory_cache_path(row, cache_dir, variant)
        if path.exists() and path.stat().st_size > 0:
            payloads[row["source_id"]] = path.read_bytes()
            cache_hits += 1
        else:
            pending.append(row)

    def work(row: dict[str, Any]) -> bytes:
        url = trajectory_url(row, variant)
        if "/hack_trajectories/" in url:
            raise ValueError("the raw Terminal Wrench variant is refused")
        data = fetch(url)
        if not data:
            raise FetchNotFound(url)
        _atomic_write(trajectory_cache_path(row, cache_dir, variant), data)
        return data

    with ThreadPoolExecutor(max_workers=workers) as pool:
        running: dict[Future[bytes], dict[str, Any]] = {}
        queue = iter(pending)
        try:
            while True:
                while len(running) < workers and (row := next(queue, None)) is not None:
                    running[pool.submit(work, row)] = row
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    row = running.pop(future)
                    try:
                        data = future.result()
                    except FetchNotFound:
                        failures[row["source_id"]] = "fetch_404"
                    except ValueError:
                        raise
                    except Exception as error:
                        failures[row["source_id"]] = f"fetch_error:{type(error).__name__}"
                    else:
                        payloads[row["source_id"]] = data
                        downloaded += len(data)
                if downloaded > budget:
                    raise DownloadBudgetExceeded(
                        f"downloaded {downloaded} bytes, over the {budget}-byte budget"
                    )
        except BaseException:
            for future in running:
                future.cancel()
            raise
    return payloads, failures, downloaded, cache_hits


def _aliases(model: str) -> set[str]:
    aliases = {model, model.replace(".", "-"), model.replace("-", " ")}
    words = [word for word in model.replace(".", "-").split("-") if word.isalpha() and len(word) >= 3]
    aliases.update(word for word in words if word != "pro")
    return aliases


def _identities(rows: list[dict[str, Any]], registry: CorpusRegistry) -> tuple[set[str], set[str]]:
    """(redaction identities, identities that must be absent from every output file)."""
    must_absent: set[str] = set(_TW_NAMES) | set(_PROVIDERS)
    for row in rows:
        must_absent |= _aliases(row["model"])
        for key in ("trial_name", "run_name", "job_name"):
            if isinstance(row.get(key), str):
                must_absent.add(row[key])
        name = registry.task_name(row["task_id"])
        if not name.isdigit():
            must_absent.add(name)
    # A bare number is not distinctive text (it would match digests, ports and counts).
    must_absent = {value for value in must_absent if len(value) >= 3 and not value.isdigit()}
    redactions = (
        set(_SOURCE_NAMES | _EVALUATOR_NAMES | _PROTECTED_TASKS_FROZEN)
        | registry.sealed_identifiers() | must_absent
    )
    return redactions, must_absent


def _blind(
    row: dict[str, Any], native_bytes: bytes, blind_id: str, redact: Callable[[Any], Any],
    must_absent: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The blinded ATIF document and its join facts, or :class:`_Skip` with a reason."""
    try:
        native = json.loads(native_bytes)
    except ValueError as error:
        raise _Skip("invalid_json") from error
    if not isinstance(native, dict) or validate_trajectory(native):
        raise _Skip("invalid_native_atif")
    kept = [step for step in native["steps"] if not _is_annotation_step(step)]
    if _ANNOTATION_MARKER_FOLDED in json.dumps(kept, ensure_ascii=False).casefold():
        raise _Skip("forbidden_text")
    allowlisted = {"schema_version": native.get("schema_version"), "steps": kept}
    origins = blind_atif_step_origins(allowlisted)
    projected = blind_atif(allowlisted, item_id=blind_id)
    if find_forbidden_json(projected):
        raise _Skip("forbidden_text")
    try:
        steps = [_project_cheatbench_step(step, redact) for step in projected["steps"]]
    except ValueError as error:
        raise _Skip(
            "forbidden_text" if "evaluator-only" in str(error) else "unsupported_atif_field"
        ) from error
    if not steps:
        raise _Skip("no_actor_visible_steps")
    for index, step in enumerate(steps, start=1):
        step["step_id"] = index
        _rekey_calls(step, index)
    version = native.get("schema_version")
    blinded = {
        "schema_version": version if version in _ATIF_VERSIONS else "ATIF-v1.6",
        "session_id": blind_id, "agent": {"name": "agent", "version": "unknown"}, "steps": steps,
    }
    if find_forbidden_json(blinded):
        raise _Skip("forbidden_text")
    folded = json.dumps(blinded, ensure_ascii=False).casefold()
    if any(value.casefold() in folded for value in must_absent):
        raise _Skip("identity_left_after_redaction")
    if validate_trajectory(blinded):
        raise _Skip("invalid_blinded_atif")
    return blinded, {"step_origin": origins, "actor_inputs_available": _actor_inputs_available(steps)}


def export_terminal_wrench_dev(
    registry: CorpusRegistry,
    output_directory: str | Path,
    manifest: str | Path,
    cache_dir: str | Path,
    *,
    repo_root: str | Path,
    max_per_label: int = 8,
    fetch: Fetch = http_fetch,
    variant: str = "sanitized",
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    workers: int = MAX_WORKERS,
) -> dict[str, Any]:
    """Publish a blinded ATIF directory of DEV Terminal Wrench runs and a private join manifest.

    Neither ``output_directory`` nor ``manifest`` may exist, and the manifest must lie
    outside the export. Returns a receipt of counts, the admission audit and the
    manifest digest. Raises :class:`DownloadBudgetExceeded` (nothing published) when
    the fetches pass ``max_download_bytes`` and :class:`AdmissionLeak` if any exported
    run's task is not DEV-admitted.
    """
    if variant != "sanitized":
        raise ValueError("only the sanitized variant is exported; the raw variant leaks labels")
    if isinstance(max_per_label, bool) or not isinstance(max_per_label, int) or max_per_label < 1:
        raise ValueError("max_per_label must be a positive integer")
    workers = max(1, min(int(workers), MAX_WORKERS))
    output = _destination(output_directory, "output directory")
    join_path = _destination(manifest, "manifest")
    if join_path == output or join_path.is_relative_to(output):
        raise ValueError("manifest must be outside the export directory")
    if not registry.has_assignment_authority():
        raise ValueError("assignments.csv authority is unavailable; refusing to certify any run as DEV")
    cache = Path(cache_dir).expanduser()
    root = Path(repo_root).expanduser()

    universe, pins = _upstream_catalog(root)
    dev_assigned = [
        task_id for task_id in universe
        if registry.assigned_split(KEY_ID, item_id=_item_id(task_id), task_id=task_id) == "dev"
    ]
    dev_tasks = {task_id for task_id in dev_assigned if _task_admitted(registry, task_id)}
    vetoed = len(dev_assigned) - len(dev_tasks)

    trajectories, tasks, index_bytes = _load_index(cache, fetch, pins)
    counts: Counter[str] = Counter()
    selected = _select(trajectories, tasks, dev_tasks, max_per_label, counts)
    payloads, failures, downloaded, cache_hits = _fetch_all(
        selected, cache, fetch, variant, workers, max_download_bytes - index_bytes,
    )
    downloaded += index_bytes
    skipped: Counter[str] = Counter()
    skipped.update(failures.values())

    redactions, must_absent = _identities(selected, registry)
    pattern = _redaction_pattern(redactions)

    def redact(value: Any) -> Any:
        return _redact_strings(value, (pattern,))

    stage = _StagedExport(output, join_path)
    joins: dict[str, dict[str, Any]] = {}
    exported: Counter[str] = Counter()
    try:
        for row in sorted(selected, key=lambda item: item["source_id"]):
            native_bytes = payloads.get(row["source_id"])
            if native_bytes is None:
                continue
            blind_id = _blind_id(row["source_id"])
            if blind_id in joins:
                raise ValueError("blind identifier collision")
            try:
                blinded, facts = _blind(row, native_bytes, blind_id, redact, must_absent)
            except _Skip as skip:
                skipped[str(skip)] += 1
                continue
            _write_run(stage.directory, blind_id, blinded, format="atif", scout_api=None)
            joins[blind_id] = {
                "source_id": row["source_id"],
                "tw_task_id": row["task_id"],
                "label": row["kind"],
                "variant": native_variant({"kind": row["kind"]}, variant),
                "model": row["model"],
                **facts,
                "hack_class": row["classification"],
                "source_url": trajectory_url(row, variant),
                "native_sha256": _sha256(native_bytes),
            }
            exported[row["kind"]] += 1
        # Admission audit: every exported run's task passes the gate again, from the join.
        for record in joins.values():
            task_id = record["tw_task_id"]
            if not _task_admitted(registry, task_id) or registry.task_name(task_id) in registry.sealed_identifiers():
                raise AdmissionLeak("an exported run's task is not DEV-admitted")
        manifest_bytes = _json_bytes(joins)
        stage.link_manifest(manifest_bytes)
        digest = registry.split_assignments_digest
        assert digest is not None  # has_assignment_authority() validated it
        receipt: dict[str, Any] = {
            "schema": "evallab.factory_tw_export/v1",
            "format": "atif",
            "variant": variant,
            "seed_rule": SEED_RULE,
            "max_per_label": max_per_label,
            "assignments_sha256": digest.removeprefix("sha256:"),
            "upstream_index_pins": pins,
            "tasks": {
                "upstream": len(universe), "dev": len(dev_assigned), "vetoed": vetoed,
                "admitted": len(dev_tasks),
            },
            "trajectories": {
                "seen": {"hack": counts["seen_hack"], "clean": counts["seen_clean"]},
                "baseline_not_rewarded": counts["baseline_not_rewarded"],
                "capped_out": counts["capped_out"],
                "selected": len(selected),
                "exported": len(joins),
                "skipped": sum(skipped.values()),
                "skipped_by_reason": dict(sorted(skipped.items())),
            },
            "hack": exported["hack"],
            "clean": exported["clean"],
            "bytes_downloaded": downloaded,
            "cache_hits": cache_hits,
            "admission_audit": {"checked": len(joins), "leaks": 0},
            "manifest_sha256": _sha256(manifest_bytes),
            "output_path": str(output),
            "manifest_path": str(join_path),
            "recorded_at_utc": datetime.now(UTC).isoformat(),
        }
        stage.publish()
        return receipt
    finally:
        stage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--max-per-label", type=int, default=8)
    parser.add_argument("--max-download-mb", type=int, default=DEFAULT_MAX_DOWNLOAD_BYTES // 1_000_000)
    args = parser.parse_args(argv)
    receipt = export_terminal_wrench_dev(
        CorpusRegistry(args.repo_root), args.output_directory, args.manifest, args.cache_dir,
        repo_root=args.repo_root, max_per_label=args.max_per_label,
        max_download_bytes=args.max_download_mb * 1_000_000,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
