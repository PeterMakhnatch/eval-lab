#!/usr/bin/env python3
"""Build the Python task ledger: one row per MiMo code task in the census pool.

Joins, per task:

* the census nop label (``../har108-python-census/task_health.parquet``);
* task-variant records (``library/task-variants``): validated repairs
  (HAR-113, HAR-115) and leak-closed variants (``leak-close-pypi@1``);
* the HAR-112 checker label (``research/explorations/trace-lab/har112/pool_labels.jsonl``);
* hand labels: HAR-111 raters 1 and 2 (``har111/census_labels.jsonl``),
  HAR-112 raters A and B and the adjudicator (``har112/hand_{a,b,adj}/``);
* the leak channel (census ``leak_channel``).

Status (Research-Harbor, HAR-115, 2026-09-30, from Peter's "document them,
either fix them or discard them... and move on"):

* ``usable``: the nop is sound on the package to run (the original, or a
  validated repair variant), no hand rater majority or adjudication calls it
  broken, one rater alone does not, and the checker does not. A
  ``pypi_fix_released`` task runs its leak-closed variant.
* ``review``: environment fine but the checker alone, or one hand rater
  alone, says broken; or the census nop is ``grader_suspect`` with no
  repair (no free reward, but the grade could not be confirmed).
* ``discarded``: ``broken_environment`` with no validated repair, or hand
  broken by two raters or by adjudication. ``reason`` says which.
* ``unchecked``: no census nop yet.

``triage`` then resolves the ``review`` rows (HAR-127 part 4): no known
repair kind fixes one, so ``grader_suspect`` rows are discarded with a
reason and a one-rater row goes the way most of its judges (hand raters and
checker) say. Checker-only rows stay ``review`` (``checker_only_instruction_gap``):
an LLM checker alone does not decide soundness (Peter, HAR-139).

Then proposes 30 ``usable`` train tasks for HAR-120: one per repository,
lightest image first, excluding held-out tasks and HAR-116's tasks. A task
whose census project key is only its own task id (no repository found) is
left out, so two picks can never be the same repository under two names;
keys are compared by last path segment, case- and ``-``/``_``-insensitive
(``github.com/psf/black`` and ``black`` are one repository).

Usage (from the checkout root):
    uv run python research/experiments/python-task-ledger/build.py
Writes ``ledger.csv`` and ``har120_proposal.csv`` next to this file and
prints the counts.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CENSUS = ROOT / "research/experiments/har108-python-census/task_health.parquet"
VARIANTS = ROOT / "library/task-variants"
TRACE = ROOT / "research/explorations/trace-lab"
CHECKER = TRACE / "har112/pool_labels.jsonl"
HAR111 = TRACE / "har111/census_labels.jsonl"
HAR112_RATERS = {"har112-a": TRACE / "har112/hand_a", "har112-b": TRACE / "har112/hand_b"}
HAR112_ADJ = TRACE / "har112/hand_adj"
REPAIR_BY = {"har113-repair", "har115-repair"}
LEAK = "leak-close-pypi@1"
#: HAR-116 Part A (the 10 HAR-110 v2 tasks) and Part B (the 5 leak tasks).
HAR116 = {
    f"format-code-task-{n}"
    for n in [
        "000383",
        "002256",
        "002391",
        "001832",
        "001896",
        "002864",
        "001161",
        "000495",
        "001181",
        "000587",
        "000226",
        "000927",
        "000146",
        "002308",
        "002402",
    ]
}
PROPOSAL_SIZE = 30
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
}
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
    "checker_label",
    "hand_labels",
    "evidence",
)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def load_variants() -> dict[str, list[tuple[dict, Path]]]:
    out: dict[str, list[tuple[dict, Path]]] = defaultdict(list)
    for path in sorted(VARIANTS.glob("*/*.json")):
        record = json.loads(path.read_text())
        out[record["task_name"].split("/", 1)[1]].append((record, path))
    return out


def load_hand() -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """Per task: rater -> label (adjudication under ``adj``), and source files."""
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    sources: dict[str, list[str]] = defaultdict(list)
    for line in HAR111.read_text().splitlines():
        row = json.loads(line)
        for rater, key in (("har111-1", "hand_label"), ("har111-2", "hand_label_rater2")):
            if row.get(key):
                labels[row["task_id"]][rater] = row[key]
                sources[row["task_id"]].append(rel(HAR111))
    for rater, folder in (*HAR112_RATERS.items(), ("adj", HAR112_ADJ)):
        for path in sorted(folder.glob("*.json")):
            labels[path.stem][rater] = json.loads(path.read_text())["label"]
            sources[path.stem].append(rel(path))
    return labels, sources


def hand_verdict(labels: dict[str, str]) -> str | None:
    """``broken`` (two raters or adjudication), ``one`` (one rater), else None."""
    if "adj" in labels:
        return "broken" if labels["adj"] == "broken" else None
    broken = sum(label == "broken" for label in labels.values())
    return "broken" if broken >= 2 else "one" if broken == 1 else None


def checker_gap(checker: dict) -> str:
    """What the HAR-112 checker says the tests need and the instruction never states."""
    items = [item for item in checker["items"] if item["severity"] == "not_inferable"]
    kinds = Counter(item["kind"] for item in items)
    broken = sum(label == "broken" for label in checker["sample_labels"])
    return (
        f"checker_only_instruction_gap (HAR-112 checker, {broken}/"
        f"{len(checker['sample_labels'])} samples broken): the tests need "
        + ", ".join(f"{kind} x{n}" for kind, n in sorted(kinds.items()))
        + f" the instruction does not state, e.g. {items[0]['test_ref']}"
    )


def triage(row: dict, checker: dict | None, hand: dict[str, str]) -> None:
    """Resolve a ``review`` row (HAR-127 part 4: fix it with a known repair kind
    or discard it with a reason).

    The known repair kinds in ``library/task-variants`` change the environment
    (``env-*``) or close a leak; none restates an instruction or confirms a
    grade, so none fixes a review row:

    * checker alone says broken: stays ``review`` as
      ``checker_only_instruction_gap``, with the checker's not-inferable item
      kinds and one test (no soundness call from the LLM checker alone; HAR-139);
    * one hand rater says broken: the majority of the judges decides, the hand
      raters and the checker (``suspect`` is not ``broken``); discarded when
      most say broken, else usable;
    * census ``grader_suspect`` with no repair: discarded, the grade cannot be
      confirmed (``REASONS`` gives each diagnosis).
    """
    if row["status"] != "review":
        return
    if row["reason"] == "HAR-112 checker says broken":
        assert checker is not None
        row["reason"] = checker_gap(checker)
    elif row["reason"] == "one hand rater says broken":
        judges = {rater: label for rater, label in hand.items() if rater != "adj"}
        if checker is not None:
            judges["checker"] = checker["label"]
        broken = sorted(judge for judge, label in judges.items() if label == "broken")
        raters = ", ".join(f"{r}={label}" for r, label in sorted(judges.items()) if r != "checker")
        summary = (
            f"{len(broken)} of {len(judges)} judges say broken (hand raters: {raters}; "
            f"HAR-112 checker: {judges.get('checker', 'none')})"
        )
        if 2 * len(broken) > len(judges):
            row["status"], row["reason"] = "discarded", summary
        else:
            row["status"], row["reason"] = "usable", f"{summary}; the majority does not"
    elif row["census_label"] == "grader_suspect":
        row["status"], row["reason"] = "discarded", f"grade unconfirmed: {row['reason']}"


def latest(records: list[tuple[dict, Path]]) -> tuple[dict, Path] | None:
    return max(records, key=lambda item: item[0]["created_at"]) if records else None


def ledger_row(
    census: dict,
    variants: list[tuple[dict, Path]],
    checker: str | None,
    hand: dict[str, str],
    hand_sources: list[str],
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
            status = "review"
            reason = REASONS.get(task_id, f"census grader_suspect: {census['evidence']}")
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
    verdict = hand_verdict(hand)
    if status == "usable":
        if verdict == "broken":
            status = "discarded"
            reason = (
                "hand-labelled broken (" + ("adjudication" if "adj" in hand else "two raters") + ")"
            )
        elif verdict == "one":
            status, reason = "review", "one hand rater says broken"
        elif checker == "broken":
            status, reason = "review", "HAR-112 checker says broken"
        elif not reason:
            reason = "nop sound" + (", leak closed" if run == "leak-closed" else "")
    if chosen is not None:
        evidence.append(rel(chosen[1]))
    evidence += [rel(path) for _, path in rejected]
    if checker is not None:
        evidence.append(f"{rel(CHECKER)}#{task_id}")
    evidence += sorted(set(hand_sources))
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
        "checker_label": checker or "",
        "hand_labels": " ".join(f"{rater}={value}" for rater, value in sorted(hand.items())),
        "evidence": " ".join(evidence),
    }


def repo_key(row: dict) -> str | None:
    """The repository a task comes from, normalised; None when unknown."""
    if row["project"].startswith("format-code-task-"):
        return None
    return row["project"].rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def propose(rows: list[dict]) -> list[dict]:
    """One task per known repository, lightest image first."""
    pool = sorted(
        (
            row
            for row in rows
            if row["status"] == "usable"
            and row["split"] == "train"
            and row["task_id"] not in HAR116
            and repo_key(row) is not None
        ),
        key=lambda row: (row["image_mib"] is None, row["image_mib"] or 0, row["task_id"]),
    )
    picked, seen = [], set()
    for row in pool:
        if repo_key(row) in seen:
            continue
        seen.add(repo_key(row))
        picked.append(row)
        if len(picked) == PROPOSAL_SIZE:
            break
    return picked


def write(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    census = pq.read_table(CENSUS).to_pylist()
    variants = load_variants()
    checker = {row["task_id"]: row for row in map(json.loads, CHECKER.read_text().splitlines())}
    hand, hand_sources = load_hand()
    rows = [
        ledger_row(
            row,
            variants.get(row["task_id"], []),
            (checker.get(row["task_id"]) or {}).get("label"),
            hand.get(row["task_id"], {}),
            hand_sources.get(row["task_id"], []),
        )
        for row in sorted(census, key=lambda row: row["task_id"])
    ]
    for row in rows:
        triage(row, checker.get(row["task_id"]), hand.get(row["task_id"], {}))
    # The HAR-120 proposal was frozen before G2 found run defects; keep it.
    proposal = [dict(row) for row in propose(rows)]
    for row in rows:
        if row["task_id"] in RUN_DEFECTS:
            digest, reason = RUN_DEFECTS[row["task_id"]]
            if row["run_digest"] != digest:
                raise SystemExit(f"{row['task_id']}: run digest changed; review RUN_DEFECTS")
            row["status"], row["reason"] = "discarded", reason
    write(HERE / "ledger.csv", rows, COLUMNS)
    write(
        HERE / "har120_proposal.csv",
        proposal,
        ("task_id", "project", "image_mib", "run", "run_digest", "run_transform", "reason"),
    )
    print("status", dict(Counter(row["status"] for row in rows)))
    print("by split", dict(Counter((row["split"], row["status"]) for row in rows)))
    print("usable run", dict(Counter(row["run"] for row in rows if row["status"] == "usable")))
    print("review", dict(Counter(row["reason"][:40] for row in rows if row["status"] == "review")))
    print(
        "discarded",
        dict(Counter(row["reason"][:40] for row in rows if row["status"] == "discarded")),
    )
    print("proposal", len(proposal), "repos", len({row["project"] for row in proposal}))


if __name__ == "__main__":
    main()
