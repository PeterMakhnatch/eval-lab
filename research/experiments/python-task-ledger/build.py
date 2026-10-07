#!/usr/bin/env python3
"""Build the Python task ledger: one row per MiMo code task in the census pool.

Status rests on mechanical evidence only (Peter, 2026-10-01: drop the
agent- and model-reviewed labels). Joins, per task:

* the census nop label (``../har108-python-census/task_health.parquet``);
* task-variant records (``library/task-variants``): validated repairs
  (HAR-113, HAR-115) and leak-closed variants (``leak-close-pypi@1``);
* defects observed in real runs (``RUN_DEFECTS``).

Status:

* ``usable``: the nop is sound on the package to run (the original, or a
  validated repair variant). A ``pypi_fix_released`` task runs its
  leak-closed variant.
* ``review``: a ``pypi_fix_released`` task with no leak-closed variant.
* ``discarded``: ``broken_environment`` with no validated repair; a census
  ``grader_suspect`` with no repair (the grade cannot be confirmed); or a
  run defect bound to the digest that showed it. ``reason`` says which.
* ``review`` also: the HAR-161 exploit probe cracked the probed digest
  (``PROBE_CRACKED``).
* ``unchecked``: no census nop yet.

Verdict (HAR-177: one keep/fix/discard per task for the Data lane).
Default-strip semantics: the sample proves ``strip-future-history@1`` removes
the future-history leak and every derive is digest-checked, so the leak is
fixed by default:

* ``discard``: status is ``discarded``.
* ``fix``: no ``strip-future-history@1`` variant, its locked nop failed
  (``STRIP_NOP_FAILED``), or an oracle label in ``ORACLE_NOT_KEEP``
  (HAR-191: unsolvable under the lock, no upstream fix, or a lenient nop).
* ``keep``: otherwise — a validated or candidate strip variant suffices.
  A validated ``purge-installed-copies@1`` whose ``repairs_digest`` is the
  current run digest becomes the run package first, so a repaired installed-copy
  leak is keep rather than the old discard.

A verifier that passes only with egress open (``oracle:fail-network``) is
discarded before the verdict. An allow-list would put hidden tests on a
network; a recorded mock has no captured bodies. ``oracle:none`` and
``nop:pass`` stay fix, not discard.

``oracle_pilot.csv`` holds the two HAR-191 pilot rows, digest-bound.
``oracle_sweep.csv`` (Work-3, when it lands) overrides the pilot per task.
An unmapped label fails the build. ``verdict_evidence`` cites the deciding
input only (evidence pointers, no prose). The verdict step does not change
``status``; the network discard and the purge switch do, before it.

LLM checker labels (HAR-111/112) and rater-agent labels do not affect
status. ``har120_proposal.csv`` is a frozen HAR-120 input and is no longer
written here.

Usage (from the checkout root):
    uv run python research/experiments/python-task-ledger/build.py
Writes ``ledger.csv`` next to this file and prints the counts.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from evallab.hardening import NETWORK_LABEL, PURGE_ID, network_discard_reason

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CENSUS = ROOT / "research/experiments/har108-python-census/task_health.parquet"
VARIANTS = ROOT / "library/task-variants"
REPAIR_BY = {"har113-repair", "har115-repair", "har146-repair", "har158-repair"}
LEAK = "leak-close-pypi@1"
STRIP = "strip-future-history@1"
#: Locked-nop failures on the strip variant: task -> failed job dirs. Empty
#: after the re-derive (10/10 re-nops pass); extend here if a future strip
#: nop fails. History: 000076's setup failed twice under the old fsck
#: exit-code gate (``runs/har177-snop-000076-8d223f9cd1fb``,
#: ``runs/har177-snop2-000076``) until the transform fix.
STRIP_NOP_FAILED: dict[str, tuple[str, ...]] = {}
#: Tasks whose strip variant needed the fixed transform (shallow-boundary
#: images): task -> passing re-nop job dir, cited in ``verdict_evidence``.
STRIP_FIXED_TRANSFORM = {
    "format-code-task-000076": ("runs/har177-r2nop-000076-80e446f0f122",),
}
#: Grading-time network fetch, repaired by pre-downloading during setup: the
#: census nop (no lock) is sound on the original, but the mandatory egress
#: lock breaks grading, so the row runs the validated prefetch variant.
PREFETCH = "env-prefetch-network@1"
#: Diagnosed defects whose census evidence alone does not say why there is no
#: repair (``research/experiments/har115-census/README.md``).
REASONS = {
    "format-code-task-000124": "fixtures expect tables Alembic migrations would create "
    "(no such table: user); no environment repair identified (har115-diag-000124)",
    "format-code-task-000393": "molecule's schema rejects the delegated driver its own "
    "tests configure (har115-diag2-000393); no known repair kind",
    "format-code-task-002848": "No module named 'src.DownloadModels': the instruction "
    "names the class, not the module; likely the agent's own work, unconfirmed",
    "format-code-task-001146": "tests run and fail on the missing feature (operation_id "
    "keyword); no count line, so the grade is unconfirmed",
    "format-code-task-000183": "pytest segfaults (exit 139) in the compiled parcels/scipy "
    "stack during the census nop (har115-nop-000183); no diagnosed environment repair",
    "format-code-task-001150": "the tests import clients/python/GirderClient.py, which is "
    "Python 2 (print statement), under Python 3.14; the image has no Python 2",
    "format-code-task-002595": "a nested pytest fails with 'LazySchema' object has no "
    "attribute 'hooks'; tests run, no environment repair identified",
}
#: Defects found in real runs after the census, bound to the exact package
#: digest that showed them: task -> (run_digest, reason). The row is
#: discarded only while the ledger still runs that digest; a different run
#: digest (e.g. a new repair variant) fails the build until reviewed.
RUN_DEFECTS = {
    "format-code-task-001269": (
        "sha256:541d416818c79503818c096a4e7ba4c2bc6ee37ba05684534a46a12464c3e18c",
        "image leaks the fix: /testbed/build/lib holds the fixed module, copied in "
        "G2 001269-a2-r2 (Traces HAR-128 labels_g2_tail; Cdx 1 HAR-127 11:01Z)",
    ),
    "format-code-task-002078": (
        "sha256:425d3bc13b8404c73571c213f66637b2360f5c692ab1e2bb767fe43a3d959cff",
        "broken under the egress lock (HAR-146 har146-lnop-002078): numpy's Cython test "
        "build fails, Cython 3.0.12 rejects checks.pyx:248 (cannot assign "
        "'NpyIter_GetMultiIndexFunc *' to 'NpyIter_GetMultiIndexFunc'); a compile error, not "
        "a network fetch, so no prefetch repair (HAR-158 diagnosis)",
    ),
}
#: HAR-161 exploit probe (``evallab probe-exploit``): an agent told not to
#: implement the fix made the hidden tests pass. Bound to the probed digest
#: like ``RUN_DEFECTS``; the row goes to ``review`` until the leak is closed.
PROBE_CRACKED = {
    "format-code-task-002402": (
        "sha256:c91c394831cfa24d2155eda1936256c06629285d472545985f54a061c6862522",
        "HAR-161 exploit probe cracked it (har161r-exploit-002402, reward 1): "
        "`git show` of unreachable upstream-fix commit 56f63eb6 in /testbed/.git "
        "(696 unreachable commits), 8 copied lines",
    ),
    "format-code-task-002552": (
        "sha256:e9273f2254788fb1803882c506c25b7274f6b908cf658d3e1065a63348f84f2d",
        "HAR-161 exploit probe cracked it (har161r-exploit-002552, reward 1): "
        "`git show` of unreachable fix commits e88159fb / edb06c52 in /testbed/.git "
        "(12 unreachable commits)",
    ),
}
#: HAR-191 labels that remove ``keep``. ``discard`` still wins, and status
#: is unchanged. ``oracle:fail`` and ``oracle:patch-conflict`` stay neutral:
#: those can be extractor misses, not a task defect.
ORACLE_NOT_KEEP = {
    "oracle:fail-network": "fix",
    "oracle:none": "fix",
    "nop:pass": "fix",
}
ORACLE_NEUTRAL = {
    "oracle:pass+nop:fail",
    "oracle:fail",
    "oracle:patch-conflict",
}
ORACLE_PILOT = HERE / "oracle_pilot.csv"
ORACLE_SWEEP = HERE / "oracle_sweep.csv"
COLUMNS = (
    "task_id",
    "split",
    "project",
    "image_mib",
    "status",
    "reason",
    "run",
    "run_digest",
    "run_transform",
    "run_variant_status",
    "census_label",
    "census_nop_job",
    "census_evidence",
    "leak_channel",
    "evidence",
    "verdict",
    "verdict_evidence",
)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def load_variants() -> dict[str, list[tuple[dict, Path]]]:
    out: dict[str, list[tuple[dict, Path]]] = defaultdict(list)
    for path in sorted(VARIANTS.glob("*/*.json")):
        record = json.loads(path.read_text())
        out[record["task_name"].split("/", 1)[1]].append((record, path))
    return out


def latest(records: list[tuple[dict, Path]]) -> tuple[dict, Path] | None:
    return max(records, key=lambda item: item[0]["created_at"]) if records else None


def strip_pick(records: list[tuple[dict, Path]]) -> tuple[dict, Path] | None:
    """Latest ``strip-future-history@1`` record for a task, if any."""
    strips = [item for item in records if item[0]["transform"] == STRIP]
    return latest(strips)


def load_oracle_rows(path: Path, *, require_digest: bool) -> dict[str, dict[str, str]]:
    """Read an oracle label table. Missing file is an empty table."""
    if not path.is_file():
        return {}
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        task_id = (row.get("task_id") or row.get("task") or "").strip()
        label = (row.get("label") or "").strip()
        if not task_id or not label:
            raise SystemExit(f"{path.name}: row missing task or label")
        if label not in ORACLE_NOT_KEEP and label not in ORACLE_NEUTRAL:
            raise SystemExit(f"{path.name}: unmapped oracle label {label!r} for {task_id}")
        evidence = (row.get("evidence") or row.get("evidence_path") or "").strip()
        if label in ORACLE_NOT_KEEP and not evidence:
            raise SystemExit(f"{path.name}: {task_id} {label} has no evidence path")
        digest = (row.get("run_digest") or "").strip()
        if require_digest and not digest:
            raise SystemExit(f"{path.name}: {task_id} missing run_digest")
        out[task_id] = {"label": label, "evidence": evidence, "run_digest": digest}
    return out


def load_oracle() -> dict[str, dict[str, str]]:
    """Pilot rows, overridden per task by ``oracle_sweep.csv`` when it lands."""
    labels = load_oracle_rows(ORACLE_PILOT, require_digest=True)
    labels.update(load_oracle_rows(ORACLE_SWEEP, require_digest=False))
    return labels


def apply_oracle(
    verdict: str,
    evidence: str,
    label: dict[str, str] | None,
) -> tuple[str, str]:
    """Override keep when the oracle label is in ``ORACLE_NOT_KEEP``."""
    if verdict == "discard" or label is None or label["label"] not in ORACLE_NOT_KEEP:
        return verdict, evidence
    return ORACLE_NOT_KEEP[label["label"]], f"{label['evidence']}:{label['label']}"


def validated_purge(
    records: list[tuple[dict, Path]],
    run_digest: str,
) -> tuple[dict, Path] | None:
    """Latest validated purge that repairs this exact run digest."""
    matches = [
        item
        for item in records
        if item[0]["transform"] == PURGE_ID
        and item[0]["status"] == "validated"
        and (item[0].get("inputs") or {}).get("repairs_digest") == run_digest
    ]
    return latest(matches)


def apply_validated_purge(
    row: dict,
    records: list[tuple[dict, Path]],
) -> tuple[dict, Path] | None:
    """Switch the run to a validated purge of the current digest."""
    chosen = validated_purge(records, row["run_digest"])
    if chosen is None:
        return None
    record, path = chosen
    row["run"] = "repair"
    row["run_digest"] = record["variant_digest"]
    row["run_transform"] = PURGE_ID
    row["run_variant_status"] = "validated"
    row["status"] = "usable"
    row["reason"] = (
        f"installed copies purged by {PURGE_ID}; oracle passed and the no-agent run failed"
    )
    row["evidence"] = (row["evidence"] + " " + rel(path)).strip()
    return chosen


def purge_repairs(records: list[tuple[dict, Path]], run_digest: str, defect_digest: str) -> bool:
    """True when the current run is a validated purge of the defect digest."""
    return any(
        item[0]["transform"] == PURGE_ID
        and item[0]["status"] == "validated"
        and item[0]["variant_digest"] == run_digest
        and (item[0].get("inputs") or {}).get("repairs_digest") == defect_digest
        for item in records
    )


def apply_network_discard(row: dict, label: dict[str, str] | None) -> None:
    """Discard a verifier that passes only with egress open."""
    if row["status"] == "discarded" or label is None or label["label"] != NETWORK_LABEL:
        return
    row["status"] = "discarded"
    row["reason"] = network_discard_reason(label["evidence"])


def verdict_for(
    status: str,
    task_id: str,
    strip: tuple[dict, Path] | None,
) -> tuple[str, str]:
    """Deterministic HAR-177 verdict plus its evidence pointer (no prose)."""
    if status == "discarded":
        return "discard", "ledger:status=discarded"
    if task_id in STRIP_NOP_FAILED:
        jobs = " ".join(STRIP_NOP_FAILED[task_id])
        return "fix", f"{jobs}:strip-nop-failed"
    if strip is not None:
        record, path = strip
        evidence = f"{rel(path)}:{STRIP}={record['status']}"
        if task_id in PROBE_CRACKED:
            evidence += f" build.py:PROBE_CRACKED#{task_id}"
        if task_id in STRIP_FIXED_TRANSFORM:
            jobs = " ".join(STRIP_FIXED_TRANSFORM[task_id])
            evidence += f" {jobs}:strip-nop-pass-fixed-transform"
        return "keep", evidence
    return "fix", f"library/task-variants:{STRIP}=absent"


def ledger_row(
    census: dict,
    variants: list[tuple[dict, Path]],
) -> dict:
    task_id = census["task_id"]
    label = census["label"]
    repairs = [
        item
        for item in variants
        if item[0]["created_by"] in REPAIR_BY and item[0]["status"] == "validated"
    ]
    leaks = [item for item in variants if item[0]["transform"] == LEAK]
    rejected = [
        item
        for item in variants
        if item[0]["created_by"] in REPAIR_BY and item[0]["status"] == "rejected"
    ]
    evidence = [f"{rel(CENSUS)}#{task_id}"]
    run, chosen = "original", None
    status, reason = "usable", ""
    if label == "unknown":
        status, reason = "unchecked", "no census nop"
    elif label != "sound":
        chosen = latest(repairs)
        if chosen is not None:
            run = "repair"
            reason = f"census {label}; repaired by {chosen[0]['transform']}, nop sound"
        elif label == "grader_suspect":
            status = "discarded"
            reason = "grade unconfirmed: " + REASONS.get(
                task_id, f"census grader_suspect: {census['evidence']}"
            )
        else:
            status = "discarded"
            tried = ", ".join(
                f"{record['transform']} rejected on its nop" for record, _ in rejected
            )
            reason = REASONS.get(task_id) or (
                f"broken environment, no known repair: {census['evidence']}"
                + (f" ({tried})" if tried else "")
            )
    if status == "usable" and chosen is None and census["leak_channel"] == "pypi_fix_released":
        chosen = latest([item for item in leaks if item[0]["status"] != "rejected"])
        if chosen is not None:
            run = "leak-closed"
        else:
            status, reason = "review", "pypi_fix_released with no leak-closed variant"
    if status == "usable" and run == "original":
        prefetched = [
            item
            for item in variants
            if item[0]["created_by"] in REPAIR_BY
            and item[0]["status"] == "validated"
            and item[0]["transform"] == PREFETCH
        ]
        if prefetched:
            chosen = latest(prefetched)
            run = "repair"
            reason = (
                f"census {label}; grading needs the network under the egress lock, "
                f"repaired by {chosen[0]['transform']}, nop sound"
            )
    if status == "usable" and not reason:
        reason = "nop sound" + (", leak closed" if run == "leak-closed" else "")
    if chosen is not None:
        evidence.append(rel(chosen[1]))
    evidence += [rel(path) for _, path in rejected]
    record = chosen[0] if chosen is not None else None
    return {
        "task_id": task_id,
        "split": census["split"],
        "project": census["project_key"],
        "image_mib": census["image_mib"],
        "status": status,
        "reason": reason,
        "run": run,
        "run_digest": record["variant_digest"] if record else census["task_version_digest"],
        "run_transform": record["transform"] if record else "",
        "run_variant_status": record["status"] if record else "",
        "census_label": label,
        "census_nop_job": census["nop_job_name"] or "",
        "census_evidence": census["evidence"] or "",
        "leak_channel": census["leak_channel"] or "",
        "evidence": " ".join(evidence),
    }


def write(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    global HERE, ROOT, CENSUS, VARIANTS, ORACLE_PILOT, ORACLE_SWEEP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Read-only evidence checkout")
    parser.add_argument("--output", type=Path, help="Ledger destination (default source ledger)")
    args = parser.parse_args()
    ROOT = args.root.resolve()
    HERE = ROOT / "research/experiments/python-task-ledger"
    CENSUS = ROOT / "research/experiments/har108-python-census/task_health.parquet"
    VARIANTS = ROOT / "library/task-variants"
    ORACLE_PILOT, ORACLE_SWEEP = HERE / "oracle_pilot.csv", HERE / "oracle_sweep.csv"
    census = pq.read_table(CENSUS).to_pylist()
    variants = load_variants()
    rows = [
        ledger_row(row, variants.get(row["task_id"], []))
        for row in sorted(census, key=lambda row: row["task_id"])
    ]
    for row in rows:
        records = variants.get(row["task_id"], [])
        apply_validated_purge(row, records)
        if row["task_id"] in RUN_DEFECTS:
            digest, reason = RUN_DEFECTS[row["task_id"]]
            if row["run_digest"] == digest:
                row["status"], row["reason"] = "discarded", reason
            elif not purge_repairs(records, row["run_digest"], digest):
                raise SystemExit(f"{row['task_id']}: run digest changed; review RUN_DEFECTS")
        elif row["task_id"] in PROBE_CRACKED:
            digest, reason = PROBE_CRACKED[row["task_id"]]
            if row["run_digest"] != digest:
                raise SystemExit(f"{row['task_id']}: run digest changed; review PROBE_CRACKED")
            row["status"], row["reason"] = "review", reason
    oracle = load_oracle()
    for row in rows:
        records = variants.get(row["task_id"], [])
        bound = oracle.get(row["task_id"])
        if bound and bound["run_digest"] and bound["run_digest"] != row["run_digest"]:
            raise SystemExit(f"{row['task_id']}: run digest changed; review oracle label")
        apply_network_discard(row, bound)
        strip = strip_pick(records)
        verdict, evidence = verdict_for(row["status"], row["task_id"], strip)
        row["verdict"], row["verdict_evidence"] = apply_oracle(verdict, evidence, bound)
        if row["verdict"] == "keep" and row["run_transform"] == PURGE_ID:
            record = next(
                item
                for item in records
                if item[0]["variant_digest"] == row["run_digest"]
            )
            row["verdict_evidence"] += f" {rel(record[1])}:{PURGE_ID}=validated"
    print("verdict", dict(Counter(row["verdict"] for row in rows)))
    print("fix", sorted(row["task_id"] for row in rows if row["verdict"] == "fix"))
    write(args.output or HERE / "ledger.csv", rows, COLUMNS)
    print("status", dict(Counter(row["status"] for row in rows)))
    print("by split", dict(Counter((row["split"], row["status"]) for row in rows)))
    print("usable run", dict(Counter(row["run"] for row in rows if row["status"] == "usable")))
    print("review", dict(Counter(row["reason"][:40] for row in rows if row["status"] == "review")))
    print(
        "discarded",
        dict(Counter(row["reason"][:40] for row in rows if row["status"] == "discarded")),
    )


if __name__ == "__main__":
    main()
