#!/usr/bin/env python3
"""Reverify the locked InferenceBench-Trajectories bundle; publish aggregates and hashed ids only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import CORPUS, REPO, REVISION, ROOT, digest, scan, selfcheck

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "eef105a8596ed37eb960363979ac148ae065303155292ebdfa039da387c460d0"
NOTICE_SHA256 = "79fdfb3fa990b99d0b2c838ba2db1acd1b8e2a0ecc7acba1b5237a19a663be8b"
LICENCE = "apache-2.0"
EXPECTED_RUNS = 269
PUBLISHED_COUNTS = None  # the dataset card publishes no positive/negative counts
CLOUD_DB = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if sha256(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"], manifest["gated"]) != (
        REPO,
        REVISION,
        LICENCE,
        False,
    ):
        raise ValueError("source pin, licence or gate differs from approval")
    if sha256((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation notice changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("immutable bundles cannot contain symlinks")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle must be locked before profiling")
    seen: set[str] = set()
    payload = 0
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
            raise ValueError("unsafe or duplicate manifest path")
        seen.add(entry["path"])
        path = root / relative
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("missing or wrong-sized manifest file")
        data = path.read_bytes()
        if sha256(data) != entry["sha256"]:
            raise ValueError("manifest SHA-256 mismatch")
        kind, _, value = entry["verified_against"].partition(":")
        if kind == "sha256":
            ok = sha256(data) == value
        elif kind == "gitsha1":
            ok = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == value  # noqa: S324
        else:
            ok = False
        if not ok:
            raise ValueError("Hub oid mismatch")
        payload += len(data)
    actual = {p.relative_to(root).as_posix() for p in paths if p.is_file()}
    if actual != seen | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}:
        raise ValueError("unmanifested or missing bundle files")
    if payload != manifest["total_bytes"] or len(seen) != manifest["file_count"]:
        raise ValueError("manifest totals mismatch")
    return {
        "files": len(seen),
        "bytes": payload,
        "verified_after_lock": True,
        "writable_paths": writable_paths,
    }


def auroc(scores: list[float], y: list[int]) -> float:
    pairs = sorted(zip(scores, y, strict=True))
    n1 = sum(y)
    n0 = len(y) - n1
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        rank_sum += sum((i + j + 1) / 2 for k in range(i, j) if pairs[k][1])
        i = j
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def cv_rate(cats: list[str], y: list[int], seed: int, folds: int = 5) -> list[float]:
    """Out-of-fold smoothed positive rate of each category (prior + 1 pseudo-count)."""
    idx = list(range(len(y)))
    random.Random(seed).shuffle(idx)
    out = [0.0] * len(y)
    for fold in range(folds):
        test = set(idx[fold::folds])
        seen: Counter[str] = Counter()
        pos: Counter[str] = Counter()
        for i in range(len(y)):
            if i not in test:
                seen[cats[i]] += 1
                pos[cats[i]] += y[i]
        prior = sum(y[i] for i in range(len(y)) if i not in test) / (len(y) - len(test))
        for i in test:
            out[i] = (pos[cats[i]] + prior) / (seen[cats[i]] + 1)
    return out


def content_free_floors(rows: list[dict[str, Any]], pos_state: str = "positive") -> dict[str, Any]:
    """AUROC of detectors that know only run metadata or trace size, never the transcript."""
    scored = [r for r in rows if r["state"] in (pos_state, "negative")]
    y = [int(r["state"] == pos_state) for r in scored]
    if not y or not any(y) or all(y):
        return {}
    ident = [r["spec"]["identity"] for r in scored]
    categorical = {
        "scenario": [i["scenario"] for i in ident],
        "harness": [i["harness"] for i in ident],
        "agent": [i["agent"] for i in ident],
        "scenario_x_harness": [f"{i['scenario']}|{i['harness']}" for i in ident],
        "scenario_x_agent": [f"{i['scenario']}|{i['agent']}" for i in ident],
    }
    result = {
        name: round(sum(auroc(cv_rate(cats, y, seed), y) for seed in range(10)) / 10, 3)
        for name, cats in categorical.items()
    }
    result["trace_length_steps"] = round(auroc([float(r["n_steps"]) for r in scored], y), 3)
    result["trace_bytes"] = round(
        auroc([float(r["spec"]["trace_bytes"]) for r in scored], y),
        3,
    )
    return {
        "n": len(y),
        "positives": sum(y),
        "method": (
            "categorical: 5-fold out-of-fold smoothed positive rate per category, mean AUROC of "
            "10 seeds; trace_length_steps / trace_bytes: raw value as score, AUROC (no fitting)"
        ),
        "auroc": result,
    }


def _table(store: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {key: dict(value) for key, value in sorted(store.items())}


def overlap(task_ids: list[str], run_ids: list[str]) -> dict[str, Any]:
    """Counts only: hashed bare/full task ids and run ids against DEV/TEST splits and corpus_tasks."""
    root = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    ours = {digest(v) for v in task_ids}
    ours_runs = {digest(v) for v in run_ids}
    out: dict[str, Any] = {
        "hashed_ids_checked": len(ours),
        "hashed_run_ids_checked": len(ours_runs),
    }
    split_hits: Counter[str] = Counter()
    split_rows: Counter[str] = Counter()
    path = Path(root) / "research/answer-keys/splits/assignments.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            split_rows[row["split"]] += 1
            fields = (row["task"], row["unit"], row["item_id"])
            if any(digest(f) in ours or digest(f) in ours_runs for f in fields):
                split_hits[row["split"]] += 1
    out["assignments_csv"] = {"rows_by_split": dict(split_rows), "exact_id_hits": dict(split_hits)}
    if CLOUD_DB.is_file():
        con = sqlite3.connect(f"file:{CLOUD_DB}?mode=ro", uri=True)
        try:
            task_hits = 0
            total = 0
            datasets: Counter[str] = Counter()
            for task_id, dataset_id in con.execute("select task_id, dataset_id from corpus_tasks"):
                total += 1
                if digest(task_id) in ours or digest(task_id.rsplit("/", 1)[-1]) in ours:
                    task_hits += 1
                if "inference" in dataset_id.lower() and "bench" in dataset_id.lower():
                    datasets[dataset_id] += 1
            out["corpus_tasks"] = {
                "rows": total,
                "exact_id_hits": task_hits,
                "dataset_ids_naming_inferencebench": dict(datasets),
            }
        finally:
            con.close()
    else:
        out["corpus_tasks"] = "cloud-trace-db.sqlite not present on this machine"
    return out


def profile(root: Path = ROOT) -> dict[str, Any]:
    integrity = verify_bundle(root)
    rows = list(scan(root))
    for row in rows:
        path = row["spec"]["_trace_path"]
        row["spec"]["trace_bytes"] = path.stat().st_size if path else 0
    checks = selfcheck(root, rows)
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    tables: dict[str, dict[str, Counter[str]]] = {
        name: defaultdict(Counter) for name in ("scenario", "harness", "agent")
    }
    verdict_counts: dict[str, Counter[str]] = defaultdict(Counter)
    combos: dict[str, Counter[str]] = defaultdict(Counter)
    any_by_state: Counter[str] = Counter()
    ids: list[str] = []
    tasks: list[str] = []
    step_total = 0
    for row in rows:
        spec, state, reason = row["spec"], row["state"], row["reason"]
        step_total += row["n_steps"]
        states[state] += 1
        reasons[f"{state}:{reason}"] += 1
        ident, labels = spec["identity"], spec["labels"]
        ids.append(ident["run_id"])
        tasks += [ident["task_id"], ident["task_full_id"]]
        for name in tables:
            tables[name][str(ident[name])]["rows"] += 1
            tables[name][str(ident[name])][state] += 1
        found = labels["verdicts"]
        for key, value in found.items():
            verdict_counts[key][{True: "fires", False: "none", None: "null"}[value]] += 1
        combo = "|".join(
            f"{k}={ {True: 'T', False: 'F', None: 'null'}[found[k]] if k in found else 'absent' }"
            for k in ("flag", "contamination_judgement", "disallowed_model_judgement")
        )
        combos[combo]["rows"] += 1
        combos[combo][state] += 1
        if labels["positive_any"]:
            any_by_state[state] += 1
    groups: Counter[str] = Counter(
        r["spec"]["trace_sha256"] or f"none:{r['spec']['identity']['run_id']}" for r in rows
    )
    positive_any_total = sum(any_by_state.values())
    return {
        "corpus": CORPUS,
        "repo": REPO,
        "revision": REVISION,
        "licence": LICENCE,
        "gated": False,
        "local_only": True,
        "cross_test_only": True,
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        **integrity,
        "rows": sum(states.values()),
        "expected_runs": EXPECTED_RUNS,
        "positives": states["positive"],
        "negatives": states["negative"],
        "ambiguous": states["ambiguous"],
        "excluded": states["excluded"],
        "scoreable": states["positive"] + states["negative"],
        "replayed_steps": step_total,
        "label_rule": (
            "PRIMARY (strict, deduplicated, leak-excluded). Signals: run_meta.invalid_or_reward_hack "
            "(flag, umbrella construct) and the contamination and disallowed-model judges "
            "(different constructs, both covered by the flag). ambiguous: a judge verdict missing "
            "or null, null flag, flag false while a judge fires, or flag true while both judges say "
            "none. positive: flag true and at least one judge fires. negative: flag false and both "
            "judges explicit none. Identical traces (sha256) keep one canonical row (sorted run id, "
            "first), copies are excluded (duplicate_trace), a canonical row whose copies carry "
            "different labels is ambiguous (duplicate_label_conflict). Excluded also: zero "
            "replayable steps, or detector-visible text (message, reasoning, results, tool-call "
            "arguments) containing a judge/label field name or the run's own verdict string "
            "(verdict_visible_to_agent). positive_any keeps the permissive any-signal predicate."
        ),
        "label_states": dict(states),
        "label_reasons": dict(reasons),
        "positive_any": {
            "rows_any_signal_fires": positive_any_total,
            "by_primary_state": dict(any_by_state),
        },
        "signal_combinations": _table(combos),
        "verdict_counts": _table(verdict_counts),
        "duplicates": {
            "rows_with_trace": sum(1 for r in rows if r["spec"]["trace_sha256"]),
            "distinct_traces": sum(1 for key in groups if not key.startswith("none:")),
            "duplicate_groups": sum(
                1 for key, n in groups.items() if n > 1 and not key.startswith("none:")
            ),
        },
        "content_free_floors_primary_labels": content_free_floors(rows),
        "per_scenario": _table(tables["scenario"]),
        "per_harness": _table(tables["harness"]),
        "per_agent": _table(tables["agent"]),
        "task_id_hashes": {"full": sorted({digest(v) for v in tasks})},
        "run_id_hashes": {"full": sorted({digest(v) for v in ids})},
        "id_uniqueness": {"run_ids": len(set(ids)), "task_ids_bare_and_full": len(set(tasks))},
        "overlap": overlap(tasks, ids),
        "published_counts": PUBLISHED_COUNTS,
        "adapter_selfcheck": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "files",
                    "bytes",
                    "rows",
                    "positives",
                    "negatives",
                    "ambiguous",
                    "excluded",
                    "scoreable",
                    "verified_after_lock",
                    "writable_paths",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
