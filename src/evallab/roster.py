"""Historical eval roster loader and read-only snapshot comparison.

Usage:
    uv run python -m evallab.roster          # print roster + drift report
    uv run python -m evallab.roster --quiet  # exit code only (0 = no differences)

The roster (policy/eval-roster.yaml) preserves the 2026-09-03 selection
snapshot, not current execution authorization or interactive agent assignments.
This module compares canary agents and live queue agents against that snapshot.
Differences are reported, never auto-fixed; see agents/CONTEXT-HUB.md §7.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _repository_root() -> Path:
    """Locate this source checkout without assuming the package nesting depth."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / ".git").exists():
            return parent
    raise SystemExit("roster: cannot locate the source checkout")


REPO_ROOT = _repository_root()
ROSTER_PATH = REPO_ROOT / "policy" / "eval-roster.yaml"


@dataclass
class Roster:
    agent: str
    model: str
    simulator_model: str
    benchmark: str
    banned: list[str]
    free_controls: list[str]
    raw: dict = field(repr=False, default_factory=dict)


def load_roster(path: Path = ROSTER_PATH) -> Roster:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover
        raise SystemExit(f"roster: cannot load {path}: {exc}") from exc
    return Roster(
        agent=raw["trial"]["agent"],
        model=raw["trial"]["model"],
        simulator_model=raw["simulator"]["model"],
        benchmark=raw["benchmark"]["primary"],
        banned=list(raw.get("banned_for_trials", [])),
        free_controls=list(raw.get("free_controls", [])),
        raw=raw,
    )


def check_drift(roster: Roster) -> list[str]:
    """Return differences from the historical snapshot, not a policy verdict."""
    findings: list[str] = []

    # 1. Canary suite agents must be roster agents or free controls.
    canary_path = REPO_ROOT / "policy" / "canary-suite.yaml"
    if canary_path.exists():
        canary = yaml.safe_load(canary_path.read_text()) or {}
        allowed = {roster.agent, *roster.free_controls}
        for agent in canary.get("agents", []) or []:
            if agent in roster.banned:
                findings.append(
                    f"canary-suite.yaml uses banned agent '{agent}' "
                    f"(roster bans: {', '.join(roster.banned)})"
                )
            elif agent not in allowed:
                findings.append(
                    f"canary-suite.yaml agent '{agent}' is not the roster trial "
                    f"agent ('{roster.agent}') — intentional? Update roster or suite."
                )

    # 2. No banned agent may hold a live (non-terminal) queue spec.
    live_states = ("pending", "approved", "waiting", "running", "proposed")
    queue_dir = REPO_ROOT / "queue"
    for state in live_states:
        for spec_file in (queue_dir / state).glob("*.json"):
            try:
                spec = json.loads(spec_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            agent = str(spec.get("agent", ""))
            if agent in roster.banned:
                findings.append(
                    f"queue/{state}/{spec_file.name}: banned agent '{agent}' "
                    f"is holding a live spec"
                )

    return findings


def main(argv: list[str] | None = None) -> int:
    quiet = bool(argv and "--quiet" in argv)
    roster = load_roster()
    findings = check_drift(roster)

    if not quiet:
        print("historical eval roster (2026-09-03)".center(60, "═"))
        print(f"  trial agent : {roster.agent}")
        print(f"  trial model : {roster.model}")
        print(f"  simulator   : {roster.simulator_model}")
        print(f"  benchmark   : {roster.benchmark}")
        print(f"  banned      : {', '.join(roster.banned) or '—'}")
        print(f"  updated     : {roster.raw.get('updated')} by {roster.raw.get('updated_by')}")
        print("  Advisory snapshot only; not current execution authorization.")
        print()
        if findings:
            print(f"⚠️  DRIFT ({len(findings)}):".center(60))
            for f in findings:
                print(f"  - {f}")
            print("\nReview against current approvals; agents/CONTEXT-HUB.md §7. Never auto-fix.")
        else:
            print("No differences in checked canary/queue agents; other consumers are not checked.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
