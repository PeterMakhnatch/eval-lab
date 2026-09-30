#!/usr/bin/env python3
"""Label every HAR-113 trial and write ``results.json``.

Each nop (``har113-vnop-*``, ``har113-rnop-*``, ``har113-nop-*``) and each
probe's verifier run is labelled by the census classifier itself
(``evallab.task_health``: ``static_checks`` on the package that ran,
``nop_evidence``, ``label_task``), so "clean" means what the census means by
``sound``. The parent's label is the census row (HAR-108's nop), or this
card's ``har113-nop-*`` when the census had none.

Probes (``har113-probe-*``) also report, from ``agent/oracle.txt``, whether
``pip download <project>==<fixed release>`` succeeded after the blocklist.
Diagnoses (``har113-diag*-*``, every round) are summarised in ``diagnosis``
by job. A repair's ``superseded`` lists earlier attempts for the task (this
card's repair records outside its final chain) with their nops.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/results.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from common import HERE, ROOT, census, original, record_path
from repair_variants import CREATED_BY

from evallab.hidden_patch import project_modules
from evallab.task_health import label_task, nop_evidence, patch_added_text, static_checks
from evallab.task_variants import load_records

RUNS = ROOT / "runs"
_PIP = re.compile(r"^== pip download (?P<spec>\S+) exit (?P<rc>\d+)", re.MULTILINE)
_BLOCKED = re.compile(r"^== blocklist applied: (?P<n>\d+) hosts", re.MULTILINE)
_HTTPS = re.compile(r"^== https (?P<url>\S+): (?P<outcome>.+)$", re.MULTILINE)


def trial_of(job: str) -> Path | None:
    trials = sorted(p for p in (RUNS / job).glob(f"{job}__*") if p.is_dir())
    return trials[-1] if trials else None


def package_of(job: str) -> Path:
    spec = json.loads((RUNS / job / "experiment-spec.json").read_text())
    task = spec.get("task") or spec["spec"]["task"]
    return (ROOT / task) if not Path(task).is_absolute() else Path(task)


def graded(job: str, package: Path | None = None) -> dict | None:
    """The census label of this job's trial, judged on the package that ran."""
    trial = trial_of(job)
    if trial is None:
        return None
    package = package or package_of(job)
    instruction = (package / "instruction.md").read_text(errors="replace")
    nop = nop_evidence(trial, instruction, project_modules(package), patch_added_text(package))
    label, reasons, evidence = label_task(static_checks(package), nop)
    return {
        "job": job,
        "trial": trial.name,
        "label": label,
        "reasons": reasons,
        "evidence": evidence,
        "reward": nop["reward"],
        "exception": nop["exception_type"],
    }


def probe(job: str) -> dict | None:
    trial = trial_of(job)
    if trial is None:
        return None
    text = (trial / "agent" / "oracle.txt").read_text(errors="replace")
    pip = _PIP.search(text)
    blocked = _BLOCKED.search(text)
    return {
        "job": job,
        "hosts_blocked": int(blocked["n"]) if blocked else None,
        "pip_download": pip["spec"] if pip else None,
        "pip_exit": int(pip["rc"]) if pip else None,
        "https": {m["url"]: m["outcome"] for m in _HTTPS.finditer(text)},
        "graded_with_blocklist": graded(job),
    }


def parent_row(task_id: str, rows: dict) -> dict:
    row = rows[task_id]
    short = task_id.removeprefix("format-code-task-")
    if row["nop_job_name"] is None and (RUNS / f"har113-nop-{short}").is_dir():
        return graded(f"har113-nop-{short}", original(task_id)) or {}
    return {
        "job": row["nop_job_name"],
        "trial": row["nop_trial_name"],
        "label": row["label"],
        "evidence": row["evidence"],
        "reward": row["nop_reward"],
    }


def main() -> None:
    rows = census()
    leak = json.loads((HERE / "leak_variants.json").read_text())["tasks"]
    repair_path = HERE / "repair_variants.json"
    repair = json.loads(repair_path.read_text())["tasks"] if repair_path.is_file() else {}
    out: dict[str, dict] = {"leak": {}, "repair": {}, "diagnosis": {}}
    for job in sorted(p.name for p in RUNS.glob("har113-vnop-*")):
        short = job.split("-")[2]
        task_id = f"format-code-task-{short}"
        out["leak"][task_id] = {
            "project": leak[task_id]["pypi_project"],
            "fixed_release": leak[task_id]["fixed_release"],
            "before": parent_row(task_id, rows),
            "after": graded(job),
            "probe_variant": probe(f"har113-probe-{short}-variant"),
            "probe_parent": probe(f"har113-probe-{short}-parent")
            if (RUNS / f"har113-probe-{short}-parent").is_dir()
            else None,
        }
    attempts: dict[str, list[str]] = {}
    for record in load_records(ROOT):
        if record.created_by == CREATED_BY:
            attempts.setdefault(record.task_name.split("/", 1)[1], []).append(record_path(record))
    for task_id, entry in sorted(repair.items()):
        short = task_id.removeprefix("format-code-task-")
        chain = {step["record"] for step in entry["steps"]}

        def nop_of(record: str, short: str = short) -> dict | None:
            job = f"har113-rnop-{short}-{Path(record).stem}"
            return graded(job) if (RUNS / job).is_dir() else None

        out["repair"][task_id] = {
            **entry,
            "before": parent_row(task_id, rows),
            "after": graded(entry["nop_job"]) if (RUNS / entry["nop_job"]).is_dir() else None,
            "step_nops": {step["record"]: nop_of(step["record"]) for step in entry["steps"][:-1]},
            "superseded": {
                record: nop_of(record)
                for record in sorted(attempts.get(task_id, []))
                if record not in chain
            },
        }
    for job in sorted(p.name for p in RUNS.glob("har113-diag*-*")):
        trial = trial_of(job)
        if trial is None or not (trial / "agent" / "oracle.txt").is_file():
            continue
        text = (trial / "agent" / "oracle.txt").read_text(errors="replace")
        listing, _, extra = text.partition("== extra:\n")
        out["diagnosis"][job] = {
            "task_id": f"format-code-task-{job.rsplit('-', 1)[1]}",
            "trial": trial.name,
            "listing": listing.splitlines()[:80],
            "extra": extra.splitlines()[:60],
        }
    (HERE / "results.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    for part in ("leak", "repair"):
        after = [v["after"]["label"] if v.get("after") else "not_run" for v in out[part].values()]
        print(part, {label: after.count(label) for label in sorted(set(after))})


if __name__ == "__main__":
    main()
