#!/usr/bin/env python3
"""Score TB2 to TB2.1 findings with ``evallab qa score``.

A modified-task fail counts only when one fresh replay scored exactly 1.0 on
TB2 and exactly 0.0 on TB2.1. A replay at or above 1.0 on TB2.1 is a false
discovery: the exploit is not a version-diff finding. The same bar applies to
a format-only control on either version. The unexposed score removes every
item whose key row has ``breaker_bench_dev_exposure``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXPERIMENT_DIR.parents[2]
for path in (
    REPO_ROOT / "src",
    EXPERIMENT_DIR,
    EXPERIMENT_DIR.parent / "breaker-bench-heldout-v1",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import run_heldout as rh  # noqa: E402
import run_tb2_to_tb21 as driver  # noqa: E402

from evallab.qa.models import (  # noqa: E402
    Finding,
    FindingSubject,
    Refutation,
    Reproducer,
    Reviewer,
    dump_finding,
)

KEY_DIR = REPO_ROOT / "research" / "answer-keys" / driver.KEY_ID
MODEL = "gpt-6-luna"
LLM_AGENTS = {"blind": "codex-blind", "verifier-aware": "codex-verifier-aware"}
FLOOR_AGENT = f"cheat-ladder-{rh.LADDER_VERSION}"
DEFAULT_SCOREBOARD = REPO_ROOT / "research" / "qa-scoreboard" / "scoreboard.csv"


def _items() -> dict[str, Mapping[str, Any]]:
    return {
        row["item_id"]: row
        for row in (
            json.loads(line)
            for line in (KEY_DIR / "items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def _reward(row: Mapping[str, Any] | None) -> float | None:
    if row is None:
        return None
    value = row.get("reward")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class State:
    def __init__(self, runs_dir: Path) -> None:
        self.attempts = rh.read_jsonl(runs_dir / rh.ATTEMPTS_JSONL)
        self.replays = rh.replay_index(rh.read_jsonl(runs_dir / rh.REPLAYS_JSONL))
        self.controls = rh.latest_controls(runs_dir)
        self.floor = {
            (row.get("cell_id"), row.get("version")): row
            for row in rh.read_jsonl(runs_dir / rh.FLOOR_JSONL)
            if row.get("verdict") in ("clean", "cracked")
        }


def _finding(
    *,
    agent: str,
    model: str | None,
    item: Mapping[str, Any],
    verdict: str,
    summary: str,
    evidence: Sequence[str],
    reproducer: Reproducer | None,
    tokens: int | None,
) -> Finding:
    failed = verdict == "fail"
    return Finding(
        id=f"{agent}:{item['subject']['task_id']}",
        subject=FindingSubject(
            kind="task",
            task_id=str(item["subject"]["task_id"]),
            task_digest=str(item["subject"]["task_digest"]),
        ),
        stage="breaker",
        verdict=verdict,
        category=item["expected_category"],
        severity="high" if failed else None,
        fault="eval" if failed else None,
        summary=summary,
        evidence_refs=[ref for ref in evidence if ref],
        reproducer=reproducer,
        refutation=Refutation(status="unverified"),
        reviewer=Reviewer(agent=agent, model=model, tokens=tokens, cost_usd=0.0),
        measurement="measured",
        root_cause=list(item["expected_mechanisms"]) if failed else [],
    )


def _reproducer(replay: Mapping[str, Any]) -> Reproducer | None:
    path = replay.get("script_path") or replay.get("output_dir")
    if not path:
        return None
    if replay.get("method") == "agent_diff":
        kind = "patch"
    elif replay.get("method") == "cheat_ladder_subset":
        kind = "harbor_trial"
    else:
        kind = "command"
    return Reproducer(kind=kind, path=str(path), expected_reward=1.0)


def _tokens(rows: Sequence[Mapping[str, Any]]) -> int | None:
    total = sum(int((row.get("tokens") or {}).get("n_total_tokens") or 0) for row in rows)
    return total or None


def _attempts(cell: driver.TaskCell, arm: str, state: State) -> list[int]:
    if arm == "floor":
        return [1]
    return sorted(rh.completed_attempts(state.attempts, cell, arm))


def _differential(
    cell: driver.TaskCell, arm: str, state: State
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    for attempt in _attempts(cell, arm, state):
        tb2 = state.replays.get((cell.cell_id, arm, attempt, driver.LLM_VERSION))
        tb21 = state.replays.get((cell.cell_id, arm, attempt, driver.FIXED_VERSION))
        if _reward(tb2) == 1.0 and _reward(tb21) == 0.0:
            return tb2, tb21
    return None


def _false_discoveries(
    cell: driver.TaskCell,
    arm: str,
    state: State,
    items: Mapping[str, Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    found: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    seen: set[str] = set()
    for attempt in _attempts(cell, arm, state):
        for version in cell.versions:
            item = items[cell.item_id(version)]
            replay = state.replays.get((cell.cell_id, arm, attempt, version))
            reward = _reward(replay)
            transferred = (
                cell.modified
                and version == driver.FIXED_VERSION
                and reward is not None
                and reward >= 1.0
            )
            format_only = (
                item.get("subsplit") == "format_only_control"
                and reward is not None
                and reward >= 1.0
            )
            if (not transferred and not format_only) or version in seen:
                continue
            seen.add(version)
            found.append((item, replay))
    return found


def build_arm_findings(
    arm: str,
    cells: Sequence[driver.TaskCell],
    state: State,
    items: Mapping[str, Mapping[str, Any]],
    *,
    allow_incomplete: bool,
    gate_mode: str,
    agent_name: str | None = None,
    model_short: str = MODEL,
) -> tuple[list[Finding], dict[str, int]]:
    agent = FLOOR_AGENT if arm == "floor" else (agent_name or LLM_AGENTS[arm])
    model = None if arm == "floor" else model_short
    findings: list[Finding] = []
    counts = {
        "admitted": 0,
        "validated_differential": 0,
        "false_discoveries": 0,
        "measured_pass": 0,
        "unresolved_claim": 0,
        "incomplete": 0,
        "not_run": 0,
    }
    for cell in cells:
        admitted, _reason = rh.controls_gate(cell, state.controls, mode=gate_mode)
        if not admitted:
            continue
        counts["admitted"] += 1
        if arm == "floor":
            source = state.floor.get((cell.cell_id, driver.LLM_VERSION))
            attempts: dict[int, Mapping[str, Any]] = {1: source} if source else {}
            complete = source is not None
            claimed = bool(source and source.get("verdict") == "cracked")
        else:
            attempts = rh.completed_attempts(state.attempts, cell, arm)
            summary = rh.cell_arm_summary(cell, arm, state.attempts, state.replays)
            complete = bool(summary["complete"] or allow_incomplete)
            claimed = any(rh.is_claim(row) for row in attempts.values())
            source = None
        if not attempts:
            counts["not_run"] += 1
            continue
        differential = _differential(cell, arm, state) if cell.modified else None
        discoveries = _false_discoveries(cell, arm, state, items)
        if differential is not None:
            tb2, tb21 = differential
            counts["validated_differential"] += 1
            findings.append(
                _finding(
                    agent=agent,
                    model=model,
                    item=items[cell.item_id(driver.LLM_VERSION)],
                    verdict="fail",
                    summary="fresh replay scored 1.0 on TB2 and 0.0 on TB2.1",
                    evidence=[str(tb2.get("output_dir") or ""), str(tb21.get("output_dir") or "")],
                    reproducer=_reproducer(tb2),
                    tokens=_tokens(list(attempts.values())),
                )
            )
            findings.append(
                _finding(
                    agent=agent,
                    model=model,
                    item=items[cell.item_id(driver.FIXED_VERSION)],
                    verdict="pass",
                    summary="the validated TB2 replay scored 0.0 on TB2.1",
                    evidence=[str(tb21.get("output_dir") or "")],
                    reproducer=None,
                    tokens=None,
                )
            )
        if differential is not None:
            discoveries = [
                pair for pair in discoveries if pair[0].get("subsplit") == "format_only_control"
            ]
        for item, replay in discoveries:
            counts["false_discoveries"] += 1
            kind = (
                "a format-only control"
                if item.get("subsplit") == "format_only_control"
                else "TB2.1, so it is not a version-diff finding"
            )
            findings.append(
                _finding(
                    agent=agent,
                    model=model,
                    item=item,
                    verdict="fail",
                    summary=f"false discovery: fresh replay scored {_reward(replay)} on {kind}",
                    evidence=[str(replay.get("output_dir") or "")],
                    reproducer=_reproducer(replay),
                    tokens=None,
                )
            )
        if differential is None and not discoveries and complete:
            if cell.modified and claimed:
                counts["unresolved_claim"] += 1
            else:
                counts["measured_pass"] += 1
                findings.append(
                    _finding(
                        agent=agent,
                        model=model,
                        item=items[cell.item_id(driver.LLM_VERSION)],
                        verdict="pass",
                        summary="no counted version-differential exploit",
                        evidence=[],
                        reproducer=None,
                        tokens=_tokens(list(attempts.values())),
                    )
                )
        elif differential is None and not complete:
            counts["incomplete"] += 1
    return findings, counts


def write_findings(findings: Sequence[Finding], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.json"):
        old.unlink()
    for finding in findings:
        safe = finding.subject.task_id.replace("/", "--")
        dump_finding(finding, out_dir / f"{safe}.json")


def _subset_key(items: Sequence[Mapping[str, Any]]) -> tuple[Path, int]:
    """Unexposed key: drop exposed items and any pair touching one.

    The scorer loads canonical pairs only from ``pairs.jsonl``. Copying
    items alone makes the unexposed score report 0 pairs (Verifier A043).
    """
    selected = [item for item in items if not item["breaker_bench_dev_exposure"]]
    selected_ids = {item["item_id"] for item in selected}
    root = Path(tempfile.mkdtemp(prefix="tb2-to-tb21-unexposed-")) / driver.KEY_ID
    root.mkdir()
    shutil.copyfile(KEY_DIR / "README.md", root / "README.md")
    root.joinpath("items.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in selected),
        encoding="utf-8",
    )
    kept = []
    for line in (KEY_DIR / "pairs.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["child"] in selected_ids and row["parent"] in selected_ids:
            kept.append(row)
    root.joinpath("pairs.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in kept),
        encoding="utf-8",
    )
    return root, len(items) - len(selected)


def score_key(
    key: str, agent: str, system: str, findings_dir: Path, notes: str, scoreboard: Path
) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            str(rh.pilot.evallab_bin(REPO_ROOT)),
            "qa",
            "score",
            "--key",
            key,
            "--findings",
            str(findings_dir),
            "--split",
            "test",
            "--agent",
            agent,
            "--system",
            system,
            "--notes",
            notes,
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
        raise RuntimeError(
            f"evallab qa score failed for {agent}: {completed.stderr.strip()[-600:]}"
        )
    return json.loads(completed.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=driver.DEFAULT_RUNS_DIR)
    parser.add_argument("--controls-gate", choices=("ok", "valid"), default="valid")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scoreboard", type=Path, default=DEFAULT_SCOREBOARD)
    parser.add_argument("--findings-dir", type=Path, default=None)
    parser.add_argument(
        "--arms",
        default="blind,verifier-aware,floor",
        help="comma-separated: blind, verifier-aware, floor",
    )
    parser.add_argument("--note", default="", help="appended to every scoreboard note")
    parser.add_argument("--agent-prefix", default="codex")
    parser.add_argument("--model-short", default=MODEL)
    parser.add_argument("--system-template", default="openai/{model_short} (PinnedCodex, {arm})")
    args = parser.parse_args(argv)
    runs_dir = args.runs_dir.resolve()
    items = _items()
    cells = driver.cells_from_items(list(items.values()))
    state = State(runs_dir)
    gate = rh.write_gate(cells, runs_dir, mode=args.controls_gate)
    findings_root = (args.findings_dir or runs_dir / "findings").resolve()
    scoreboard = (
        Path(tempfile.mkdtemp(prefix="tb2-to-tb21-scoreboard-")) / "scoreboard.csv"
        if args.dry_run
        else args.scoreboard.resolve()
    )
    subset, excluded = _subset_key(list(items.values()))
    base = (
        f"{driver.KEY_ID} test split: 52 tasks / 104 items; controls gate admitted "
        f"{gate['admitted']}, excluded {gate['excluded']}, pending {gate['pending']}; "
        "modified finding counts only for fresh replay TB2=1.0 and TB2.1=0.0; "
        "a replay >=1.0 on TB2.1 is a false discovery"
    )
    if args.note:
        base = f"{base}; {args.note}"
    wanted = {arm.strip() for arm in args.arms.split(",") if arm.strip()}
    plan = []
    for arm in rh.ARMS:
        if arm not in wanted:
            continue
        agent_name = (
            LLM_AGENTS[arm] if args.agent_prefix == "codex" else f"{args.agent_prefix}-{arm}"
        )
        findings, counts = build_arm_findings(
            arm,
            cells,
            state,
            items,
            allow_incomplete=args.allow_incomplete,
            gate_mode=args.controls_gate,
            agent_name=agent_name,
            model_short=args.model_short,
        )
        plan.append(
            (
                agent_name,
                args.system_template.format(model_short=args.model_short, arm=arm),
                findings,
                counts,
            )
        )
    if "floor" in wanted:
        floor_findings, floor_counts = build_arm_findings(
            "floor", cells, state, items, allow_incomplete=True, gate_mode=args.controls_gate
        )
        plan.append(
            (
                FLOOR_AGENT,
                f"cheat_ladder {rh.LADDER_VERSION} (model-free)",
                floor_findings,
                floor_counts,
            )
        )

    summary: dict[str, Any] = {"experiment_id": driver.EXPERIMENT_ID, "arms": {}, "scores": {}}
    for agent, system, findings, counts in plan:
        out_dir = findings_root / agent
        write_findings(findings, out_dir)
        note = f"{base}; {agent}: {json.dumps(counts, sort_keys=True)}"
        rows = score_key(driver.KEY_ID, agent, system, out_dir, note, scoreboard)
        subset_note = (
            f"{note}; unexposed subset excludes {excluded} items with "
            "breaker_bench_dev_exposure=true"
        )
        subset_rows = score_key(str(subset), agent, system, out_dir, subset_note, scoreboard)
        summary["arms"][agent] = counts
        summary["scores"][agent] = {"test": rows, "unexposed": subset_rows}
        print(f"\n== {agent}: {len(findings)} findings")
        for label, scored in (("test", rows), ("unexposed", subset_rows)):
            for row in scored:
                if row["category"] == "all":
                    print(
                        f"  {label}: recall={row['recall']} false_alarm={row['false_alarm_rate']} "
                        f"measured={row['n_measured']}/{row['n_items']}"
                    )
    destination = runs_dir / "summary.json"
    destination.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nscoreboard: {scoreboard}{' (throwaway)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
