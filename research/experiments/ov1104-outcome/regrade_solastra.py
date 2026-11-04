"""Re-execute the Sol/Astra safeguard rollouts' final workspaces in a fresh container.

The 56 replay-set Sol/Astra runs (42 live + 14 scripted) were graded in their own containers by
the task's `tests/verify.py` (visible functional tests on pristine copies + hidden probes + tamper
and summary checks). This harness rebuilds each task's environment image, uploads the run's
final `/testbed` workspace (kept on disk by the rollout drivers), copies the task's `tests/` to
`/tests` and runs `tests/test.sh` fresh, so `functional_pass` (the visible suite = the "original"
grader a naive harness would trust) and `reward` (the independent verifier) are fresh
observations.

Usage::

    .venv/bin/python research/experiments/ov1104-outcome/regrade_solastra.py \
        --manifest ~/Developer/eval-lab-results/line/replay-v1/inputs/corpora/sol-astra-manifest.jsonl \
        --work /tmp/w2sr/work --work /tmp/w3sf/work --out /tmp/ov1104-outcome/regrade-solastra
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from evallab.ref_subset import _check, _docker

SCHEMA = "ov1104-outcome.regrade-solastra/v1"
REPO = Path(__file__).resolve().parents[3]
TASK_DIRS = {
    "alert-sink-backfill": "research/experiments/ov-safeguard-integrity-v1-20261031/tasks/alert-sink-backfill",
    "serving-path-migration": "research/experiments/ov-safeguard-integrity-v1-20261031/tasks/serving-path-migration",
    "ci-gate-stabilization": "research/experiments/ov-safeguard-integrity-v1-20261031/tasks/ci-gate-stabilization",
    "delegated-preflight-refactor": "research/experiments/ov-safeguard-integrity-v1-20261031/tasks/delegated-preflight-refactor",
    "sol-sink-permission-friction": "research/experiments/ov-safeguard-friction-v2-20261031/tasks/sol-sink-permission-friction",
    "astra-strict-schema-friction": "research/experiments/ov-safeguard-friction-v2-20261031/tasks/astra-strict-schema-friction",
}


def build_image(task: str, task_dir: Path) -> str:
    tag = f"ov1104-outcome-solastra/{task}:{hashlib.sha256(str(task_dir).encode()).hexdigest()[:8]}"
    _check(_docker("build", "-q", "-t", tag, str(task_dir / "environment"), timeout=900), f"build {task}")
    return tag


def regrade(image: str, task_dir: Path, workspace: Path, label: str, *, memory: str, timeout: float = 600.0) -> dict[str, Any]:
    name = f"ov1104-solastra-{label[:40]}-{int(time.time() * 1000) % 100000}"
    started = time.monotonic()
    try:
        _check(_docker("run", "-d", "--name", name, "--network", "none", "--memory", memory, "--cpus", "1", "--entrypoint", "/bin/sh", image, "-c", "exec tail -f /dev/null"), "docker run")
        _check(_docker("exec", name, "sh", "-c", "rm -rf /testbed && mkdir -p /testbed /tests /logs/verifier"), "reset workdir")
        _check(_docker("cp", f"{workspace}/.", f"{name}:/testbed/"), "upload workspace")
        _check(_docker("cp", f"{task_dir / 'tests'}/.", f"{name}:/tests/"), "copy tests")
        try:
            result = _docker("exec", name, "bash", "/tests/test.sh", timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "reward": None, "seconds": round(time.monotonic() - started, 1)}
        reward_raw = _docker("exec", name, "cat", "/logs/verifier/reward.txt").stdout.decode().strip()
        report_raw = _docker("exec", name, "cat", "/logs/verifier/safeguard_report.json").stdout.decode()
        try:
            report = json.loads(report_raw)
        except json.JSONDecodeError:
            report = None
        try:
            reward = float(reward_raw)
        except ValueError:
            reward = None
        return {
            "status": "scored" if reward is not None else "unscored",
            "reward": reward,
            "rc": result.returncode,
            "functional_pass": (report or {}).get("functional_pass"),
            "safeguard_preserved": (report or {}).get("safeguard_preserved"),
            "test_tampered": (report or {}).get("test_tampered"),
            "summary_dishonest": (report or {}).get("summary_dishonest"),
            "seconds": round(time.monotonic() - started, 1),
            "tail": result.stdout.decode(errors="replace")[-1500:],
        }
    except (RuntimeError, OSError) as exc:
        return {"status": "error", "reward": None, "seconds": round(time.monotonic() - started, 1), "tail": str(exc)[-1500:]}
    finally:
        _docker("rm", "-f", name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work", type=Path, action="append", required=True, help="rollout work roots holding <id>/testbed")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--memory", default="2g")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    images: dict[str, str] = {}
    receipts = []
    for row in rows:
        run_id, task = row["id"], row["task_id"]
        workspace = next((w / run_id / "testbed" for w in args.work if (w / run_id / "testbed").is_dir()), None)
        stored = json.loads((Path(row["path"]) / "grade.json").read_text())
        sr = stored.get("safeguard_report") or {}
        receipt: dict[str, Any] = {"run_id": run_id, "task": task, "stored_reward": stored.get("reward"), "stored_functional_pass": sr.get("functional_pass"), "workspace": str(workspace) if workspace else None}
        if workspace is None:
            receipt["fresh"] = {"status": "workspace_missing", "reward": None}
        else:
            task_dir = REPO / TASK_DIRS[task]
            if task not in images:
                images[task] = build_image(task, task_dir)
            receipt["image"] = images[task]
            receipt["fresh"] = regrade(images[task], task_dir, workspace, run_id, memory=args.memory)
        receipts.append(receipt)
        (args.out / "receipts.json").write_text(json.dumps({"schema": SCHEMA, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "runs": receipts}, indent=1))
        f = receipt["fresh"]
        print(json.dumps({"run": run_id, "stored": (receipt["stored_functional_pass"], receipt["stored_reward"]), "fresh": (f.get("functional_pass"), f.get("reward"), f.get("status"))}), flush=True)
    for tag in images.values():
        _docker("rmi", tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
