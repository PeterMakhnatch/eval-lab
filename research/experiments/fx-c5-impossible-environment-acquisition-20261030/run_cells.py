#!/usr/bin/env python3
"""C5 executed validation: honest vs cheat solution on the unhardened package, on Modal.

Two cells, one per arm, both through ``ModalBackend.run_container_trial`` with
``mode="shortcut"``: the solver script runs in the agent container, then the
package's own ``tests/test.sh`` runs natively in a separate verifier container.
The native reward is the measured number. Cap $0.50.

    uv sync --extra laminar
    uv run --extra laminar python research/experiments/fx-c5-impossible-environment-acquisition-20261030/run_cells.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.qa.task_modal import ModalBackend  # noqa: E402
from evallab.registry import task_directory_digest  # noqa: E402
from evallab.regrade import CloudSpendGate  # noqa: E402

TASK = HERE / "tasks" / "glyph2-decode"
ARMS = ("honest", "cheat")
OUT = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-30/fx-c5-impossible-environment-acquisition"
)
RECEIPTS = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-30-factory/ds-gold/raw/C5/receipts"
)
CAP = 0.50
APPROVAL = "Peter 2026-10-30 factory budget: C5 impossible-task validation, Modal cap $0.50"
TAG = "fx-c5-impossible"
LEDGER = Path.home() / ".local/state/evallab-failure-library/fx-c5-modal-ledger.json"
CPU_S, GIB_S = 0.00003942, 0.00000667  # docs/execution-tiers.md Modal Sandbox rates


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def estimate(package: Path, wall: float) -> float:
    env = tomllib.loads((package / "task.toml").read_text()).get("environment", {})
    cpus = int(env.get("cpus", 2))
    memory = int(env.get("memory_mb", 8192))
    return (cpus * CPU_S + memory / 1024 * GIB_S) * wall * 1.25


def run_arm(arm: str, backend: ModalBackend, meter: dict) -> dict:
    cell = OUT / "cells" / arm
    result_path = cell / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    cell.mkdir(parents=True, exist_ok=True)
    script = (HERE / "solutions" / arm / "solve.sh").read_text()
    projected = estimate(TASK, 600)
    if meter["spent"] + projected > CAP:
        raise SystemExit(f"cap ${CAP:.2f} would be passed (spent ${meter['spent']:.3f})")
    started = time.monotonic()
    observation = backend.run_container_trial(
        TASK,
        digest=task_directory_digest(TASK),
        mode="shortcut",
        script=script,
        trial_dir=cell / "trial",
        timeout_seconds=900,
    )
    wall = time.monotonic() - started
    cost = estimate(TASK, wall)
    meter["spent"] += cost
    meter["cells"] += 1
    trial = cell / "trial"
    paths = {
        "agent_log": trial / "agent" / "oracle.txt",
        "test_stdout": trial / "verifier" / "test-stdout.txt",
        "test_output": trial / "verifier" / "test_output.log",
        "reward_file": trial / "verifier" / "reward.txt",
    }
    texts = {
        name: (path.read_text(errors="replace") if path.exists() else "")
        for name, path in paths.items()
    }
    result = {
        "arm": arm,
        "status": observation.status,
        "reason": observation.reason,
        "reward": observation.facts.get("reward"),
        "wall_s": round(wall, 1),
        "est_usd": round(cost, 5),
        "script_sha256": sha256(script.encode()),
        "package_digest": task_directory_digest(TASK),
        "evidence_sha256": sha256((texts["agent_log"] + texts["test_stdout"]).encode()),
        "agent_log_tail": texts["agent_log"][-1200:],
        "test_stdout_tail": texts["test_stdout"][-1500:],
        "cloud_spend": observation.facts.get("cloud_spend"),
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    copy_receipts(arm, result, texts)
    return result


def copy_receipts(arm: str, result: dict, texts: dict[str, str]) -> None:
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        if not text:
            continue
        path = RECEIPTS / f"{arm}-{name}.log"
        path.write_text(text)
    (RECEIPTS / f"{arm}-cell.json").write_text(json.dumps(result, indent=2) + "\n")
    digest_file = RECEIPTS / f"{arm}-sha256.txt"
    lines = []
    for path in sorted(RECEIPTS.glob(f"{arm}-*")):
        if path.name == digest_file.name:
            continue
        lines.append(f"{sha256(path.read_bytes())[:12]}  {path.name}")
    digest_file.write_text("\n".join(lines) + "\n")


def main() -> None:
    gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=CAP,
        usd_per_execution=0.0025,
        spend_ledger=LEDGER,
        modal_app_name=TAG,
        billing_tag=TAG,
    )
    backend = ModalBackend(repo_root=REPO, spend_gate=gate)
    meter = {"spent": 0.0, "cells": 0}
    results = []
    for arm in ARMS:
        results.append(run_arm(arm, backend, meter))
        last = results[-1]
        print(
            f"{arm}: status={last['status']} reward={last['reward']} "
            f"wall={last['wall_s']}s est=${last['est_usd']} reason={last['reason']}",
            flush=True,
        )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"estimated_usd": round(meter["spent"], 4), "cells": meter["cells"]}))


if __name__ == "__main__":
    main()
