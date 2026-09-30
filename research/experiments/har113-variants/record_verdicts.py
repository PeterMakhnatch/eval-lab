#!/usr/bin/env python3
"""Append HAR-113's nop evidence to the variant records (``results.json``).

- A leak variant from the nop sample is ``validated`` when its nop gets the
  parent's census label and the probe's ``pip download`` of the fixed
  release fails with the blocklist applied (graded as its nop). It stays a
  ``candidate``, with the evidence appended, when the parent is itself
  broken: the leak is closed but the task is not admissible.
- A repair variant is ``validated`` when its nop is ``sound``, ``rejected``
  when it is ``broken_environment``, and otherwise (``grader_suspect``: the
  environment held but the nop's result is not a clean fail) stays a
  ``candidate`` with the evidence appended. Earlier steps of a chained
  repair, and earlier attempts replaced by a retry, are ``rejected`` on
  their own nop as superseded.

Leak variants outside the sample stay ``candidate`` with no evidence.
Re-running skips records whose verdict is already final.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/record_verdicts.py
"""

from __future__ import annotations

import json

from common import HERE, ROOT, record_path, records_by
from leak_variants import TRANSFORM as LEAK_TRANSFORM

from evallab.task_variants import append_status_evidence, resolve_record

BY = "har113-variants"


def trial(graded: dict) -> str:
    return f"runs/{graded['job']}/{graded['trial']}"


def verdict(path: str, status: str, evidence: str, by: str = BY) -> str:
    """Append ``evidence`` unless the record is final or already has it."""
    current = resolve_record(ROOT / path, repo_root=ROOT)
    if current.status != "candidate" or any(e.evidence == evidence for e in current.evidence):
        return current.status
    append_status_evidence(path, status, evidence=evidence, by=by, repo_root=ROOT)
    return status


def main() -> None:
    results = json.loads((HERE / "results.json").read_text())
    leaks = records_by(LEAK_TRANSFORM)
    counts: dict[str, int] = {}
    for task_id, row in results["leak"].items():
        before, after, probe = row["before"], row["after"], row["probe_variant"]
        blocked = probe["pip_exit"] not in (None, 0)
        graded = probe["graded_with_blocklist"]["label"]
        evidence = (
            f"nop {trial(after)}: {after['label']} (parent nop {before['job']}: "
            f"{before['label']}); leak probe runs/{probe['job']}: pip download "
            f"{probe['pip_download']} exit {probe['pip_exit']} with the blocklist applied, "
            f"grading with the blocklist {graded}"
        )
        same = after["label"] == before["label"] == graded
        if not (same and blocked):
            status = "rejected"
        else:
            status = "validated" if after["label"] == "sound" else "candidate"
        final = verdict(record_path(leaks[task_id]), status, evidence)
        counts[f"leak {final}"] = counts.get(f"leak {final}", 0) + 1
    for row in results["repair"].values():
        after = row["after"]
        if after is None:
            continue
        evidence = (
            f"nop {trial(after)}: {after['label']} ({after['evidence']}); parent nop "
            f"{row['before'].get('job')}: {row['before'].get('label')}"
        )
        if after["label"] in ("sound", "broken_environment"):
            status = "validated" if after["label"] == "sound" else "rejected"
        else:
            status = "candidate"
        final = verdict(row["record"], status, evidence)
        counts[f"repair {final}"] = counts.get(f"repair {final}", 0) + 1
        earlier = {
            step["record"]: row["step_nops"].get(step["record"]) for step in row["steps"][:-1]
        }
        for record, nop in {**earlier, **row["superseded"]}.items():
            if nop:
                verdict(
                    record,
                    "rejected",
                    f"nop {trial(nop)}: {nop['label']} ({nop['evidence']}); superseded by "
                    f"{row['record']}",
                )
    print(counts)


if __name__ == "__main__":
    main()
