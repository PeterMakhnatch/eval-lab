"""Replay the shipped watch integrity-board policy on trace-paired-v1 dev only.

No model, sandbox, stop actuator, or detector fitting. Score the generated
qa.finding/v1 predictions with the existing ``evallab qa score`` command.
Missing ATIFs remain unknown, including oracle and scripted CheatAgent trials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from evallab.harbor_watch_hooks import read_hook_journal
from evallab.live_watch import WatchThresholds, evaluate_alerts, trial_signals
from evallab.qa.answer_key import item_id_of, load_answer_key
from evallab.qa.models import Finding, Reviewer
from evallab.results_home import results_root
from evallab.storage.paths import shared_checkout_root
from evallab.watch_stop import INTEGRITY_BOARD_RULES

ROOT = Path(__file__).resolve().parents[3]
KEY = ROOT / "research/answer-keys/trace-paired-v1"


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run(out: Path) -> dict:
    key = load_answer_key(KEY)
    thresholds = WatchThresholds()
    now = datetime.now(UTC)
    published = results_root()
    local = shared_checkout_root(ROOT)
    reviewer = Reviewer(agent="TA monitor", tokens=0, cost_usd=0)
    findings: list[dict] = []
    observations: list[dict] = []
    for item in key.items:
        if item.split != "dev":
            continue
        extra = item.model_extra or {}
        ref = extra["trial_ref"]
        if ref["kind"] not in {"published", "local_run"}:
            raise ValueError(f"unsupported trial reference: {ref['kind']}")
        trial = (published if ref["kind"] == "published" else local) / ref["path"]
        trajectory = trial / "agent/trajectory.json"
        if not trajectory.is_file():
            trajectory = trial / "trajectory.json"
        usable = trajectory.is_file() and (trial / "result.json").is_file()
        alerts: list[dict] = []
        signal = None
        if usable:
            signal = trial_signals(
                trial.parent,
                trial,
                thresholds=thresholds,
                from_config=True,
                now=now.timestamp(),
                hooks=read_hook_journal(trial.parent).get(trial.name),
            )
            usable = signal["steps"] > 0
            if usable:
                alerts = evaluate_alerts(signal, thresholds=thresholds)
        integrity = [alert for alert in alerts if alert["rule"] in INTEGRITY_BOARD_RULES]
        reason = "measured" if usable else "no usable completed ATIF"
        finding = Finding(
            subject=item.subject,
            stage="T0-live-watch-integrity-board",
            verdict=("fail" if integrity else "pass") if usable else "unknown",
            category="reward_hacking",
            severity="high" if integrity else None,
            fault="agent" if integrity else None,
            summary=(
                "Candidate integrity alert; not independently verified cheating"
                if integrity
                else "No integrity-board alert; not a clean-trace certificate"
            ) if usable else reason,
            evidence_refs=[
                f"{trajectory}#{alert['step_ref']}" if alert["step_ref"] else str(trajectory)
                for alert in integrity
            ],
            reviewer=reviewer,
            measurement="measured" if usable else "ran_without_usable_result" if signal else "not_run",
        )
        findings.append(finding.model_dump(mode="json", by_alias=True))
        observations.append({
            "item_id": item_id_of(key.key_id, item),
            "trial_id": item.subject.trial_id,
            "role": item.role,
            "split": item.split,
            "trial_path": str(trial),
            "trajectory_sha256": digest(trajectory) if trajectory.is_file() else None,
            "result_sha256": digest(trial / "result.json") if (trial / "result.json").is_file() else None,
            "measurement": finding.measurement,
            "reason": reason,
            "steps": signal["steps"] if signal else None,
            "integrity_flag": bool(integrity) if usable else None,
            "alerts": [
                {field: alert[field] for field in ("rule", "severity", "step_ref")}
                for alert in alerts
            ],
        })
    manifest = {
        "schema": "live-watch.paired-dev/v1",
        "created_at_utc": now.isoformat(),
        "runtime_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "key": key.key_id,
        "split": "dev",
        "key_sha256": {name: digest(KEY / name) for name in ("items.jsonl", "pairs.jsonl")},
        "watch_source_sha256": {
            name: digest(ROOT / "src/evallab" / name)
            for name in ("live_watch.py", "watch_stop.py", "tripwires.py", "edit_signals.py")
        },
        "integrity_rules": list(INTEGRITY_BOARD_RULES),
        "thresholds": asdict(thresholds),
        "items": len(observations),
        "measured": sum(row["integrity_flag"] is not None for row in observations),
        "scope": "Retrospective dev label agreement, not a held-out result or cheating certification",
        "stop_mode": "off: no stop actuator or capture API is invoked",
        "llm_calls": 0,
        "new_native_trials": 0,
        "new_cost_usd": 0,
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, rows in (("findings.jsonl", findings), ("observations.jsonl", observations)):
        (out / name).write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New immutable output directory")
    args = parser.parse_args()
    print(json.dumps(run(args.out), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
