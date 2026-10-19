#!/usr/bin/env python3
"""Score Track A weak TB2.1 breaker runs against ``weak-tests-generalization-20261019``.

Counts a task as cracked (``verdict="fail"``) only when a candidate program:
1. Scores ``1.0`` on pristine ``tb21`` in a fresh container,
2. Scores ``0.0`` on pristine ``hardened_tb21`` in a fresh container (where ``hardened_tb21``
   is verified ``oracle=1.0, nop=0.0`` and kills 12/12 BenchmarkAuditor mutant witnesses), and
3. Violates an explicit requirement of ``instruction.md`` (verified by inspecting the
   replay script / diff and ``breaker_report``).

Root-cause attribution is non-tautological: a finding's ``root_cause`` includes a
BenchmarkAuditor ``expected_root_causes`` string only when the candidate program or
``breaker_report`` demonstrably exploits that specific assertion gap (as opposed to an
orthogonal shortcut such as copying/overwriting ``/app/correct_output.csv`` or writing
a static final ``.DAT`` snapshot).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXPERIMENT_DIR.parents[2]
for _path in (
    REPO_ROOT / "src",
    EXPERIMENT_DIR,
    EXPERIMENT_DIR.parent / "breaker-bench-heldout-v1",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import run_heldout as rh  # noqa: E402  # ty: ignore[unresolved-import]
import run_tb21_weak_breaker as driver  # noqa: E402  # ty: ignore[unresolved-import]

from evallab.qa.models import (  # noqa: E402
    Finding,
    FindingSubject,
    Refutation,
    Reproducer,
    Reviewer,
    dump_finding,
)

DEFAULT_SCOREBOARD = REPO_ROOT / "research" / "qa-scoreboard" / "scoreboard.csv"
DEFAULT_RESULTS_JSON = EXPERIMENT_DIR / "results.json"


def _load_all_key_items() -> dict[str, dict[str, Any]]:
    return {
        str(row["item_id"]): row
        for row in (
            json.loads(line)
            for line in driver.KEY_ITEMS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def _reward(row: Mapping[str, Any] | None) -> float | None:
    if row is None:
        return None
    val = row.get("reward")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _trial_cost_and_tokens(runs_dir: Path, job_name: str | None) -> tuple[float, int]:
    if not job_name:
        return 0.0, 0
    for root in (runs_dir, REPO_ROOT / "runs"):
        job_dir = root / job_name
        if not job_dir.is_dir():
            continue
        for traj in job_dir.glob("*/agent/trajectory.json"):
            try:
                data = json.loads(traj.read_text(encoding="utf-8"))
                fm = data.get("final_metrics") or {}
                cost = float(fm.get("total_cost_usd") or 0.0)
                toks = int(fm.get("total_prompt_tokens") or 0) + int(
                    fm.get("total_completion_tokens") or 0
                )
                return cost, toks
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
    return 0.0, 0


def _read_replay_artifact_text(replay_row: Mapping[str, Any]) -> str:
    sp = replay_row.get("script_path")
    if not sp:
        return ""
    path = Path(str(sp))
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _audit_instruction_violation_and_root_cause(
    cell: driver.WeakTB21Cell,
    attempt_row: Mapping[str, Any],
    weak_replay: Mapping[str, Any],
    hard_replay: Mapping[str, Any],
) -> tuple[bool, str, list[str]]:
    """Verify the candidate violates instruction.md and classify its root cause."""
    if _reward(weak_replay) != 1.0 or _reward(hard_replay) != 0.0:
        return False, "differential replay did not score 1.0 -> 0.0", []

    report = attempt_row.get("breaker_report") or {}
    summary = str(report.get("summary") or "")
    hyp = str(report.get("mechanism_hypothesis") or "")
    mech = str(report.get("mechanism") or "verifier_shortcut")
    script_txt = _read_replay_artifact_text(weak_replay)
    combined = f"{summary}\n{hyp}\n{script_txt}".lower()

    slug = cell.slug
    if slug == "cobol-modernization":
        # Check whether candidate exploited missing invalid-owner testing vs static final snapshot
        if "valid_owner" in combined or "invalid-owner" in combined or "book.owner" in combined:
            return (
                True,
                "Candidate omits/weakens book ownership validation required by program.cbl.",
                [
                    "test_program_output replays three transactions that all have a valid owner; no invalid-owner input is graded."
                ],
            )
        return (
            True,
            "Candidate writes hardcoded final .DAT snapshot without implementing program.cbl transaction processing on arbitrary INPUT.DAT.",
            [
                f"breaker:{mech}:test_program_output checks only final .DAT snapshot after one fixed 3-input sequence"
            ],
        )

    if slug == "multi-source-data-merger":
        causes: list[str] = []
        if (
            '"total_conflicts": 1' in script_txt
            or "'total_conflicts': 1" in script_txt
            or "only checks" in combined
            or (
                "email" in combined
                and ("created_date" not in script_txt or "john d." not in combined)
            )
        ):
            causes.append(
                "test_conflict_report_values only checks the user-101 email conflict; name and created_date conflict records are unchecked."
            )
        if (
            "if email_conflicts" in combined
            or 'field": ""' in script_txt
            or "empty field" in combined
        ):
            causes.append(
                "test_conflict_report_values guards its value checks with `if email_conflicts:`; an empty field name skips them."
            )
        if not causes:
            causes.append(f"breaker:{mech}:incomplete_merge_or_conflict_report")
        return (
            True,
            "Candidate fabricates unchecked user rows (102/103) and/or omits unchecked name and created_date conflicts in /app/conflicts.json.",
            causes,
        )

    if slug == "sam-cell-seg":
        if "from mobile_sam" not in script_txt and "import mobile_sam" not in script_txt:
            causes = [f"breaker:{mech}:staged_correct_output_or_non_sam_geometry"]
            if (
                "xmin" in combined
                and "xmax" in combined
                and (
                    "no test compares" in combined or "bbox" in combined or "unmodified" in combined
                )
            ):
                causes.append(
                    "Tests check shape, columns, polyline geometry, IoU and overlap from coords_x/coords_y; no test compares xmin/xmax/ymin/ymax with the polyline."
                )
            return (
                True,
                "Candidate bypasses MobileSAM refinement (copying/overwriting staged correct_output.csv or emitting synthetic polygons without updating bbox columns from MobileSAM).",
                causes,
            )
        return (
            True,
            "Candidate leaves xmin/xmax/ymin/ymax inconsistent with refined polylines.",
            [
                "Tests check shape, columns, polyline geometry, IoU and overlap from coords_x/coords_y; no test compares xmin/xmax/ymin/ymax with the polyline."
            ],
        )

    if slug == "sqlite-db-truncate":
        causes = []
        if (
            "score > 6" in combined
            or "7" in combined
            or "seven" in combined
            or "soln_tuples & data_tuples" in combined
            or "intersection" in combined
        ):
            causes.append(
                "test_json_data passes when score > 6 matching rows, so up to two wrong or missing rows pass."
            )
        if not causes:
            causes.append(f"breaker:{mech}:test_json_data_partial_or_superset_match")
        return (
            True,
            "Candidate exploits test_json_data set-intersection / score > 6 threshold without accurately recovering only the 8 valid rows from trunc.db.",
            causes,
        )

    raise ValueError(f"unexpected slug {slug}")


def score_arm(
    *,
    runs_dir: Path,
    arm: str,
    agent_name: str,
    system_label: str,
    scoreboard: Path,
    note_prefix: str,
) -> dict[str, Any]:
    items = _load_all_key_items()
    cells = driver.cells_from_key()
    attempts = rh.read_jsonl(runs_dir / rh.ATTEMPTS_JSONL)
    replays = rh.replay_index(rh.read_jsonl(runs_dir / rh.REPLAYS_JSONL))
    controls = rh.latest_controls(runs_dir)

    findings: list[Finding] = []
    per_task: list[dict[str, Any]] = []
    admitted_n = 0
    validated_n = 0
    root_cause_hit_n = 0
    total_cost_usd = 0.0
    total_tokens = 0

    for cell in cells:
        admitted, gate_reason = rh.controls_gate(cell, controls, mode="ok")
        if not admitted:
            per_task.append({"slug": cell.slug, "admitted": False, "gate_reason": gate_reason})
            continue
        admitted_n += 1
        cell_attempts = rh.completed_attempts(attempts, cell, arm)
        cell_cost = 0.0
        cell_tokens = 0
        for row in cell_attempts.values():
            c, t = _trial_cost_and_tokens(runs_dir, str(row.get("job_name") or ""))
            cell_cost += c
            cell_tokens += t
        total_cost_usd += cell_cost
        total_tokens += cell_tokens

        winning_tuple: (
            tuple[int, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str, list[str]]
            | None
        ) = None
        for att_num in sorted(cell_attempts):
            att_row = cell_attempts[att_num]
            w_rep = replays.get((cell.cell_id, arm, att_num, driver.LLM_VERSION))
            h_rep = replays.get((cell.cell_id, arm, att_num, driver.FIXED_VERSION))
            if w_rep is None or h_rep is None:
                continue
            violated, viol_summary, causes = _audit_instruction_violation_and_root_cause(
                cell, att_row, w_rep, h_rep
            )
            if violated:
                winning_tuple = (att_num, att_row, w_rep, h_rep, viol_summary, causes)
                break

        item = items[cell.item_id()]
        expected_causes = set(item.get("expected_root_causes") or [])
        if winning_tuple is not None:
            att_num, att_row, w_rep, h_rep, viol_summary, causes = winning_tuple
            validated_n += 1
            rc_hit = bool(set(causes) & expected_causes)
            if rc_hit:
                root_cause_hit_n += 1
            rep_path = str(w_rep.get("script_path") or w_rep.get("output_dir") or "")
            findings.append(
                Finding(
                    id=f"{agent_name}:{item['subject']['task_id']}",
                    subject=FindingSubject(
                        kind="task",
                        task_id=str(item["subject"]["task_id"]),
                        task_digest=str(item["subject"]["task_digest"]),
                    ),
                    stage="breaker",
                    verdict="fail",
                    category="weak",
                    severity="high",
                    fault="eval",
                    summary=viol_summary,
                    evidence_refs=[
                        str(w_rep.get("output_dir") or ""),
                        str(h_rep.get("output_dir") or ""),
                        rep_path,
                    ],
                    reproducer=Reproducer(
                        kind="command" if w_rep.get("method") != "agent_diff" else "patch",
                        path=rep_path,
                        expected_reward=1.0,
                    )
                    if rep_path
                    else None,
                    refutation=Refutation(status="unverified"),
                    reviewer=Reviewer(
                        agent=agent_name,
                        model=driver.DEFAULT_MODEL,
                        tokens=cell_tokens,
                        cost_usd=round(cell_cost, 6),
                    ),
                    measurement="measured",
                    root_cause=causes,
                )
            )
            per_task.append(
                {
                    "slug": cell.slug,
                    "admitted": True,
                    "attempts_run": len(cell_attempts),
                    "winning_attempt": att_num,
                    "weak_tb21_reward": _reward(w_rep),
                    "hardened_tb21_reward": _reward(h_rep),
                    "instruction_violated": True,
                    "root_cause_match": rc_hit,
                    "named_root_causes": causes,
                    "expected_root_causes": list(expected_causes),
                    "mechanism": (att_row.get("breaker_report") or {}).get("mechanism"),
                    "summary": viol_summary,
                    "replay_script": rep_path,
                    "cost_usd": round(cell_cost, 6),
                    "tokens": cell_tokens,
                }
            )
        else:
            findings.append(
                Finding(
                    id=f"{agent_name}:{item['subject']['task_id']}",
                    subject=FindingSubject(
                        kind="task",
                        task_id=str(item["subject"]["task_id"]),
                        task_digest=str(item["subject"]["task_digest"]),
                    ),
                    stage="breaker",
                    verdict="pass",
                    category="weak",
                    severity=None,
                    fault=None,
                    summary="no replay-validated instruction-violating exploit on weak_tb21",
                    evidence_refs=[],
                    reproducer=None,
                    refutation=Refutation(status="unverified"),
                    reviewer=Reviewer(
                        agent=agent_name,
                        model=driver.DEFAULT_MODEL,
                        tokens=cell_tokens,
                        cost_usd=round(cell_cost, 6),
                    ),
                    measurement="measured",
                    root_cause=[],
                )
            )
            per_task.append(
                {
                    "slug": cell.slug,
                    "admitted": True,
                    "attempts_run": len(cell_attempts),
                    "winning_attempt": None,
                    "weak_tb21_reward": None,
                    "hardened_tb21_reward": None,
                    "instruction_violated": False,
                    "root_cause_match": False,
                    "cost_usd": round(cell_cost, 6),
                    "tokens": cell_tokens,
                }
            )

    out_dir = runs_dir / "findings" / agent_name
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.json"):
        old.unlink()
    for f in findings:
        safe = f.subject.task_id.replace("/", "--")
        dump_finding(f, out_dir / f"{safe}.json")

    note = (
        f"{note_prefix}; admitted={admitted_n}/4 confirmed weak TB2.1 tasks, "
        f"validated_differential={validated_n}/{admitted_n} (weak_tb21=1.0 -> hardened_tb21=0.0 + instruction violation), "
        f"root_cause_match={root_cause_hit_n}/{validated_n or 1}, "
        f"total_cost_usd=${total_cost_usd:.4f}"
    )
    tmp_key_dir = Path(tempfile.mkdtemp(prefix="weak-tests-key-")) / driver.KEY_ID
    tmp_key_dir.mkdir(parents=True, exist_ok=True)
    (tmp_key_dir / "README.md").write_text(
        f"# {driver.KEY_ID}\n\nSource: research/answer-keys/{driver.KEY_ID}\n",
        encoding="utf-8",
    )
    (tmp_key_dir / "items.jsonl").write_text(
        driver.KEY_ITEMS_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_key_dir / "findings.jsonl").write_text(
        driver.KEY_FINDINGS_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(rh.pilot.evallab_bin(REPO_ROOT)),
            "qa",
            "score",
            "--key",
            str(tmp_key_dir),
            "--findings",
            str(out_dir),
            "--split",
            "dev",
            "--agent",
            agent_name,
            "--system",
            system_label,
            "--notes",
            note,
            "--scoreboard",
            str(scoreboard),
            "--json",
        ],
        cwd=REPO_ROOT,
        env=rh.pilot.build_subprocess_env(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"evallab qa score failed: {completed.stderr}")
    score_rows = json.loads(completed.stdout)
    return {
        "agent": agent_name,
        "system": system_label,
        "admitted": admitted_n,
        "validated_differential": validated_n,
        "recall": round(validated_n / admitted_n, 4) if admitted_n else None,
        "root_cause_match": round(root_cause_hit_n / validated_n, 4) if validated_n else None,
        "total_cost_usd": round(total_cost_usd, 6),
        "total_tokens": total_tokens,
        "per_task": per_task,
        "score_rows": score_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=driver.DEFAULT_RUNS_DIR)
    parser.add_argument("--struct-runs-dir", type=Path, default=None)
    parser.add_argument("--scoreboard", type=Path, default=DEFAULT_SCOREBOARD)
    parser.add_argument("--results-json", type=Path, default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    scoreboard = (
        Path(tempfile.mkdtemp(prefix="tb21-weak-sb-")) / "scoreboard.csv"
        if args.dry_run
        else args.scoreboard.resolve()
    )
    runs_dir = args.runs_dir.resolve()
    mutant_check = {}
    if (runs_dir / driver.MUTANT_CHECK_JSON).is_file():
        mutant_check = json.loads((runs_dir / driver.MUTANT_CHECK_JSON).read_text(encoding="utf-8"))

    arm1 = score_arm(
        runs_dir=runs_dir,
        arm="verifier-aware",
        agent_name="codex-verifier-aware",
        system_label="openrouter-metered/openai/gpt-oss-120b (terminus-2, verifier-aware)",
        scoreboard=scoreboard,
        note_prefix="Track A E-2 weak TB2.1 breaker (standard verifier-aware + fresh-container differential replay vs hardened_tb21)",
    )
    arms_out = {"verifier-aware": arm1}

    if args.struct_runs_dir and args.struct_runs_dir.resolve().is_dir():
        arm2 = score_arm(
            runs_dir=args.struct_runs_dir.resolve(),
            arm="verifier-aware",
            agent_name="codex-verifier-aware-structure-only",
            system_label="openrouter-metered/openai/gpt-oss-120b (terminus-2, structure-only verifier-aware)",
            scoreboard=scoreboard,
            note_prefix="Track A E-2 weak TB2.1 breaker (structure-only /eval_env/tests + fresh-container differential replay vs hardened_tb21)",
        )
        arms_out["verifier-aware-structure-only"] = arm2

    cracked_by_slug: dict[str, dict[str, Any]] = {}
    for arm_key, arm_res in arms_out.items():
        for row in arm_res["per_task"]:
            if row.get("instruction_violated") and row["slug"] not in cracked_by_slug:
                cracked_by_slug[row["slug"]] = {"arm": arm_key, **row}
    admitted_total = arm1["admitted"]
    union_validated = len(cracked_by_slug)
    union_rc_hits = sum(1 for r in cracked_by_slug.values() if r.get("root_cause_match"))
    union_cost = sum(a["total_cost_usd"] for a in arms_out.values())
    union_tokens = sum(a["total_tokens"] for a in arms_out.values())
    payload = {
        "schema": "evallab.tb21_weak_breaker.results/v1",
        "experiment_id": driver.EXPERIMENT_ID,
        "key_id": driver.KEY_ID,
        "mutant_witness_check": mutant_check,
        "arms": arms_out,
        "union": {
            "admitted": admitted_total,
            "validated_differential": union_validated,
            "recall": round(union_validated / admitted_total, 4) if admitted_total else None,
            "root_cause_match": round(union_rc_hits / union_validated, 4)
            if union_validated
            else None,
            "total_cost_usd": round(union_cost, 6),
            "total_tokens": union_tokens,
            "cracked_tasks": cracked_by_slug,
        },
    }
    args.results_json.resolve().write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
