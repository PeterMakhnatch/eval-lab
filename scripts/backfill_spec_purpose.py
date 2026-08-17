#!/usr/bin/env python3
"""One-time backfill of `ExperimentSpec.purpose` into an existing queue.

`purpose` became required (`docs/build-plan.md` WS-E item 1), and `queue/` is
live runtime state written before the field existed. Every reader validates a
spec on load and `DirectoryQueue.list_specs` deliberately re-raises anything
that is not a vanished file, so one pre-`purpose` file makes the nightly canary
(`canary.py:61`, which scans all eight states), the nightly researcher
(`researchers.py:884`), and the digest's waiting block (`digest.py:259`) raise.
That fail-closed behaviour is correct and is not weakened; this script is the
migration it implies.

Run it once, per checkout:

    uv run python scripts/backfill_spec_purpose.py            # dry run, default
    uv run python scripts/backfill_spec_purpose.py --apply

**It derives intent from evidence and never invents a research claim.** A
purpose is read as research intent by `evallab preflight` and by purpose-scoped
budgeting, so relabelling a historical canary as a "baseline" would corrupt both.
Where a spec self-identifies — `policy_rule: canary`, `submitted_by: judge`,
`submitted_by: autopilot-researcher`, a reserved `smoke-` name — the value comes
from that. Where it carries no such evidence, the fallback is applied and the
record is **reported separately** so it can be corrected rather than trusted.

The fallback is `practice` because it is the only member of the taxonomy that
asserts no measurement about an agent. That is a gap in the taxonomy, not a good
fit: see `agents/handoffs/spine-purpose.md`, which escalates it to Peter with
`selftest` named as the value that would fit. Because `queue/` is gitignored,
rebuildable state, a wrong value here is correctable by editing the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.schemas import EXPERIMENT_PURPOSES, ExperimentSpec  # noqa: E402

#: Applied when a record carries no evidence of its own intent. Every file that
#: lands here is reported, because an unreported guess is the failure mode.
FALLBACK_PURPOSE = "practice"

#: The eight queue state directories, plus the researcher's proposal area. Kept
#: literal rather than imported from `queue.QUEUE_STATES` so this script can run
#: against an archived queue whose state set differs from today's code.
STATE_DIRS = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
)


@dataclass
class BackfillReport:
    """What the run did, in enough detail to be checked afterwards."""

    updated: list[Path] = field(default_factory=list)
    undeclared: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    already: list[Path] = field(default_factory=list)


def derive_purpose(raw: dict) -> tuple[str, str | None]:
    """A purpose plus the evidence it came from, or the fallback and `None`.

    Ordered most specific first. Each rule cites a producer in `src/evallab`, so
    a reader can check the claim rather than trust the mapping.
    """
    policy_rule = raw.get("policy_rule") or ""
    submitted_by = raw.get("submitted_by") or ""
    task = raw.get("task") or ""
    name = raw.get("name") or ""

    if policy_rule == "canary" or task.startswith("canary/"):
        # `canary.py` submits pinned repeats to detect change in the lab itself.
        return "drift", "canary submission (policy_rule=canary or task=canary/*)"
    if submitted_by == "judge" or task.startswith("registered/judge-"):
        # `calibrate.queued_calibration_spec` measures a judge against a corpus.
        return "calibration", f"judge calibration (submitted_by={submitted_by!r}, task={task!r})"
    if submitted_by == "autopilot-researcher":
        # `researchers._write_proposed_spec`: a one-variable contrast.
        return "comparison", "autopilot researcher proposal (one-variable contrast)"
    if submitted_by in {"solidify-smoke", "speed-profile"} or name.startswith("smoke-"):
        # `smoke.py` / `scripts/profile/harness.py`: not a research claim.
        return "practice", f"lab self-test (submitted_by={submitted_by!r}, name={name!r})"
    if raw.get("agent") in {"oracle", "nop"}:
        # Not a guess, and not a fallback: `AGENTS.md` states outright that
        # "`oracle` and `nop` are the default local controls. They test task and
        # harness validity; they are not evidence of model capability." A record
        # that cannot be evidence about a model cannot be a baseline,
        # comparison, or elicitation result, which leaves the non-measuring
        # member of the taxonomy.
        return "practice", (
            f"free local control (agent={raw.get('agent')!r}) — AGENTS.md: controls "
            "test task and harness validity, not model capability"
        )
    return FALLBACK_PURPOSE, None


def _looks_like_a_spec(raw: object) -> bool:
    """Whether a parsed file is an `ExperimentSpec` document at all.

    A queue state directory holds only specs today, but this script also runs
    against archived trees. Selecting positively avoids rewriting a neighbour.
    """
    if not isinstance(raw, dict):
        return False
    required = ("name", "hypothesis", "task", "agent", "submitted_by")
    return all(isinstance(raw.get(key), str) for key in required)


def _with_purpose(raw: dict, purpose: str) -> dict:
    """`purpose` inserted after `hypothesis`, matching the model's field order."""
    out: dict = {}
    for key, value in raw.items():
        out[key] = value
        if key == "hypothesis":
            out["purpose"] = purpose
    if "purpose" not in out:
        out["purpose"] = purpose
    return out


def backfill_queue(queue_root: Path, *, apply: bool = False) -> BackfillReport:
    """Add a derived `purpose` to every spec in `queue_root` that lacks one."""
    report = BackfillReport()
    for state in STATE_DIRS:
        state_dir = queue_root / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                report.skipped.append(path)
                continue
            if not _looks_like_a_spec(raw):
                report.skipped.append(path)
                continue
            if raw.get("purpose") is not None:
                report.already.append(path)
                continue

            purpose, evidence = derive_purpose(raw)
            candidate = _with_purpose(raw, purpose)
            try:
                # Never write a file this lab would then refuse to read.
                ExperimentSpec.model_validate(candidate)
            except Exception:
                report.skipped.append(path)
                continue

            report.updated.append(path)
            if evidence is None:
                report.undeclared.append(path)
            if apply:
                path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n")
    return report


def _print(report: BackfillReport, *, queue_root: Path, apply: bool) -> None:
    verb = "updated" if apply else "would update"
    print(f"queue: {queue_root}")
    print(f"{verb}: {len(report.updated)}")
    for path in report.updated:
        raw = json.loads(path.read_text())
        purpose, evidence = derive_purpose(raw) if raw.get("purpose") is None else (
            raw["purpose"],
            "already applied by this run",
        )
        print(f"  {path.parent.name}/{path.name} -> {purpose}")
        print(f"    evidence: {evidence or 'NONE — fallback applied, please check'}")
    print(f"already declared: {len(report.already)}")
    print(f"skipped (not a spec, or would still be invalid): {len(report.skipped)}")
    for path in report.skipped:
        print(f"  {path.parent.name}/{path.name}")
    if report.undeclared:
        print()
        print(
            f"{len(report.undeclared)} record(s) carried no evidence of intent and got "
            f"{FALLBACK_PURPOSE!r}. These are guesses — correct them by editing the "
            "file, or leave them if the value is acceptable:"
        )
        for path in report.undeclared:
            print(f"  {path.parent.name}/{path.name}")
    if not apply and report.updated:
        print()
        print("dry run: nothing was written. Re-run with --apply.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=REPO_ROOT / "queue",
        help="queue root to migrate (default: ./queue)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes; without it this is a dry run",
    )
    args = parser.parse_args(argv)

    if not args.queue.is_dir():
        print(f"no queue directory at {args.queue}; nothing to migrate")
        return 0
    report = backfill_queue(args.queue, apply=args.apply)
    _print(report, queue_root=args.queue, apply=args.apply)
    assert all(purpose in EXPERIMENT_PURPOSES for purpose in (FALLBACK_PURPOSE,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
