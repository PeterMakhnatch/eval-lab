#!/usr/bin/env python3
"""Bounded, model-free defect probes through Eval Lab's native oracle matrix API.

Run the two previously refused 002391 patches and the corrected 000169/001269
probes. Historical successful controls in results.json are retained, not rerun.
Each oracle script applies the recorded/reference patch, runs the review probe,
and leaves the original hidden grader to Harbor. A failed probe is an observed
negative, not an oracle setup failure; patch/preparation failures remain infra.
Full native logs live under runs/, outside git, and every case holds the host
Docker FIFO slot through cleanup. No task variant or missing ATIF is fabricated.

  uv run python research/experiments/qa-trace/defect-review/run_probes.py
  uv run python research/experiments/qa-trace/defect-review/run_probes.py --task 002391
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shlex
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.docker_lock import docker_slot  # noqa: E402
from evallab.execution_contracts import new_ulid  # noqa: E402
from evallab.mimo_clean import SNAPSHOT_REL  # noqa: E402
from evallab.qa.counterfactual import reference_patch, strip_binary_artifacts  # noqa: E402
from evallab.registry import compute_task_digests  # noqa: E402
from evallab.results_home import results_root  # noqa: E402
from evallab.runner import run_matrix  # noqa: E402
from evallab.schemas import ExperimentMatrix, MatrixRun  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

TASKS = shared_checkout_root(REPO) / SNAPSHOT_REL / "tasks"
KEY = REPO / "research" / "answer-keys" / "trace-paired-v1" / "items.jsonl"
JOBS = "runs"


def _trial_diff(trial_id: str) -> str:
    """Drop only the bytecode/binary-artifact stanzas the @10 collector drops."""
    for line in KEY.read_text().splitlines():
        item = json.loads(line)
        if item["subject"]["trial_id"] == trial_id:
            path = results_root() / item["trial_ref"]["path"] / "verifier" / "agent.diff"
            return strip_binary_artifacts(path.read_text())[0]
    raise KeyError(trial_id)


PIP_AUDIT = (
    "PYTHONPATH=/workspace/repo PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
    "python -m pytest usercase-test-coderl/test_qa_probe_alias_exclusion.py "
    "-v -p no:cacheprovider --junitxml=/logs/agent/qa-probe.xml"
)
HPCCM = (
    "python3 -m pytest test/test_qa_probe_include.py "
    "-v -p no:cacheprovider --junitxml=/logs/agent/qa-probe.xml"
)
RESPONSES = (
    "python3 -m pytest responses/tests/test_qa_probe_activate_strict.py "
    "-v -p no:cacheprovider --junitxml=/logs/agent/qa-probe.xml"
)

CASES: list[dict] = [
    {
        "case": "002391 primary id kept out of aliases",
        "task": "format-code-task-002391",
        "probe": "test_qa_probe_alias_exclusion.py",
        "dest": "usercase-test-coderl/test_qa_probe_alias_exclusion.py",
        "command": PIP_AUDIT,
        "changes": {
            "flagged parent har104-d-002391__WxBjcjX": lambda: _trial_diff("har104-d-002391__WxBjcjX"),
            "child chrr1-l-cheat-002391-k1__QGXLXwa": lambda: _trial_diff("chrr1-l-cheat-002391-k1__QGXLXwa"),
        },
    },
    {
        "case": "000169 included building block reused; examples build",
        "task": "format-code-task-000169",
        "probe": "test_qa_probe_include.py",
        "dest": "test/test_qa_probe_include.py",
        "command": HPCCM,
        "prepare": ["dev-switch python3 3.11", "python3 -m pip install -e . --no-input"],
        "examples": {
            "reference fix": {
                "recipes/examples/cloverleaf.py": [
                    "FROM ubuntu:18.04", "g++",
                    "https://github.com/UK-MAC/CloverLeaf_Serial.git", "cp clover_leaf /usr/local/bin",
                ],
            },
            "flagged parent ovn-g5-000169-gepa__Hmxf7wb": {
                "recipes/examples/include.py": ["FROM ubuntu:16.04", "g++"],
            },
            "child ovn-g5-000169-tuned__CxBpceW": {},
        },
        "changes": {
            "reference fix": lambda: reference_patch("mimo-v2.6-rl/format-code-task-000169", REPO),
            "flagged parent ovn-g5-000169-gepa__Hmxf7wb": lambda: _trial_diff("ovn-g5-000169-gepa__Hmxf7wb"),
            "child ovn-g5-000169-tuned__CxBpceW": lambda: _trial_diff("ovn-g5-000169-tuned__CxBpceW"),
        },
    },
    {
        "case": "001269 separate strict decorator (prompt option 1) vs hidden tests",
        "task": "format-code-task-001269",
        "probe": "test_qa_probe_activate_strict.py",
        "dest": "responses/tests/test_qa_probe_activate_strict.py",
        "command": RESPONSES,
        "alt": "alt_activate_strict.py",
        "changes": {"alternative design: responses.activate_strict": lambda: ""},
    },
]


def _write_payload(path: str, text: str) -> str:
    encoded = base64.b64encode(text.encode()).decode()
    return (
        "python3 - <<'QA_PAYLOAD'\nfrom pathlib import Path\nimport base64\n"
        f"p = Path({path!r})\np.parent.mkdir(parents=True, exist_ok=True)\n"
        f"p.write_bytes(base64.b64decode({encoded!r}))\nQA_PAYLOAD"
    )


def _script(case: dict, label: str, change: str, workdir: str) -> str:
    steps = ["#!/usr/bin/env bash", "set -euo pipefail", f"cd {shlex.quote(workdir)}"]
    steps += [_write_payload("/tmp/qa-review-change.diff", change)]
    if change:
        steps += [
            "git apply --check --whitespace=nowarn /tmp/qa-review-change.diff",
            "git apply --whitespace=nowarn /tmp/qa-review-change.diff",
        ]
    if case.get("alt"):
        steps += [
            _write_payload("/tmp/qa-review-alternative.py", (HERE / "probes" / case["alt"]).read_text()),
            "python3 /tmp/qa-review-alternative.py",
        ]
    steps += case.get("prepare", [])
    steps += [_write_payload(case["dest"], (HERE / "probes" / case["probe"]).read_text())]
    examples = json.dumps(case.get("examples", {}).get(label, {}))
    steps += [f"export PROBE_EXAMPLE_EXPECTATIONS={shlex.quote(examples)}"]
    # The controller writes the receipt from the subprocess return value, never
    # from a grepped echo or a pipeline's final command. Pytest 1 is a measured
    # assertion failure; no report / collection errors remain unmeasured.
    steps += [
        "python3 - <<'QA_EXECUTE'\n"
        "import json, subprocess, sys\nfrom pathlib import Path\n"
        f"command = {case['command']!r}\n"
        "log = Path('/logs/agent/qa-probe.log')\n"
        "receipt = Path('/logs/agent/qa-probe.json')\n"
        "try:\n"
        "    done = subprocess.run(['bash', '-c', command], stdout=subprocess.PIPE,\n"
        "                          stderr=subprocess.STDOUT, text=True, timeout=300)\n"
        "except subprocess.TimeoutExpired as exc:\n"
        "    out = exc.stdout or b''\n"
        "    if isinstance(out, bytes): out = out.decode(errors='replace')\n"
        "    log.write_text(out)\n"
        "    receipt.write_text(json.dumps({'command': command, 'exit_code': None, 'error': 'timeout'}))\n"
        "    print(out, end='')\n    sys.exit(1)\n"
        "log.write_text(done.stdout)\nprint(done.stdout, end='')\n"
        "receipt.write_text(json.dumps({'command': command, 'exit_code': done.returncode}))\n"
        "if done.returncode not in (0, 1): sys.exit(1)\n"
        "QA_EXECUTE\n"
    ]
    return "\n".join(steps) + "\n"


def _hidden_result(native: dict) -> bool | None:
    rewards = native.get("rewards", [])
    if native.get("status") not in {"ok", "mismatch"} or len(rewards) != 1:
        return None
    reward = rewards[0]
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
        return None
    return reward == 1.0


def run_one(case: dict, label: str, change: str, scratch: Path) -> dict:
    """One native oracle job, with useful full logs and fail-closed setup."""
    if not isinstance(change, str):
        raise ValueError(f"{label}: no validated patch is available")
    task = TASKS / case["task"]
    config = tomllib.loads((task / "task.toml").read_text())
    workdir = config["environment"]["workdir"]
    digests = compute_task_digests(task)
    script = _script(case, label, change, workdir)
    nonce = new_ulid().lower()[-10:]
    name = f"defect-{case['task'].rsplit('-', 1)[-1]}-{nonce}"
    scratch = scratch / name
    scratch.mkdir(parents=True, exist_ok=False)
    (scratch / "solve.sh").write_text(script)
    (scratch / "change.diff").write_text(change)
    script_hash = hashlib.sha256(script.encode()).hexdigest()
    solution = REPO / "runs/qa-trace-defect-review/scripts" / f"{script_hash}.sh"
    solution.parent.mkdir(parents=True, exist_ok=True)
    solution.write_text(script)
    matrix = ExperimentMatrix(
        matrix_id=new_ulid(), name=name,
        hypothesis="Compare a prompt-derived semantic probe with the task's unchanged hidden grader.",
        benchmark_family="qa-trace-defect-review", task_id=case["task"],
        task=str(task), task_package_digest=digests.package, verifier_digest=digests.verifier,
        environment="docker", jobs_dir=JOBS, concurrency=1, timeout_seconds=1800,
        runs=[MatrixRun(name=name, agent="oracle", solution=solution.relative_to(REPO).as_posix())],
    )
    (scratch / "matrix.json").write_text(matrix.model_dump_json(indent=2) + "\n")
    with docker_slot(label=f"qa-defect {case['task']} {label}", slots=1):
        execution = run_matrix(matrix, root=REPO)
    native = execution.results[0]
    job = REPO / JOBS / name
    trials = [p.parent for p in job.glob("*/result.json")]
    trial = trials[0] if len(trials) == 1 else None
    receipt = trial / "agent" / "qa-probe.json" if trial else None
    observation = json.loads(receipt.read_text()) if receipt and receipt.is_file() else {}
    xml = trial / "agent" / "qa-probe.xml" if trial else None
    tests = list(ET.parse(xml).getroot().iter("testcase")) if xml and xml.is_file() else []
    failures = [t.attrib.get("name") for t in tests if t.find("failure") is not None]
    errors = [t.attrib.get("name") for t in tests if t.find("error") is not None]
    rc = observation.get("exit_code")
    measured = (
        bool(tests) and not errors and observation.get("command") == case["command"]
        and ((rc == 0 and not failures) or (rc == 1 and bool(failures)))
    )
    row = {
        "case": case["case"], "task": case["task"], "change": label,
        "observed_at": datetime.now(UTC).isoformat(), "execution": "native_oracle_matrix",
        "probe_sha256": hashlib.sha256((HERE / "probes" / case["probe"]).read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "change_sha256": hashlib.sha256(change.encode()).hexdigest(),
        "task_package_digest": digests.package, "verifier_digest": digests.verifier,
        "probe_status": ("passed" if rc == 0 else "failed") if measured else "refused",
        "probe_pass": rc == 0 if measured else None,
        "probe_exit_code": rc, "probe_tests": len(tests), "probe_failures": failures,
        "probe_errors": errors, "hidden_pass": None,
        "native_result": native, "matrix_receipt": str(execution.receipt_path),
        "job_dir": str(job), "trial_dir": str(trial) if trial else None,
        "probe_log": str(trial / "agent" / "qa-probe.log") if trial else None,
        "script_evidence": str(scratch / "solve.sh"),
    }
    row["hidden_pass"] = _hidden_result(native)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="Only cases whose task name contains this string")
    args = parser.parse_args()
    # Post-processing is unrelated to these review probes; retain native jobs
    # without writing dashboard/catalog projections from a delegated worker.
    os.environ["EVALLAB_NO_PROCESS_JOB"] = "1"
    results = HERE / "results.json"
    rows = json.loads(results.read_text())
    refused = False
    for case in CASES:
        if args.task and args.task not in case["task"]:
            continue
        for label, get_change in case["changes"].items():
            change = get_change()
            if not isinstance(change, str):
                raise ValueError(f"{label}: no validated patch is available")
            config = tomllib.loads((TASKS / case["task"] / "task.toml").read_text())
            script = _script(case, label, change, config["environment"]["workdir"])
            script_hash = hashlib.sha256(script.encode()).hexdigest()
            if any(
                r.get("script_sha256") == script_hash
                and r.get("probe_status") in {"passed", "failed"}
                and (
                    r.get("hidden_pass") is not None
                    or _hidden_result(r.get("native_result") or {}) is not None
                )
                for r in rows
            ):
                continue
            row = run_one(case, label, change, Path("/private/tmp/qa-trace-defect-review-native"))
            rows.append(row)
            results.write_text(json.dumps(rows, indent=1) + "\n")
            print(f"{row['case']} | {label} | probe={row['probe_status']} hidden={row['hidden_pass']}", flush=True)
            refused |= row["probe_status"] == "refused" or row["hidden_pass"] is None
    return int(refused)


if __name__ == "__main__":
    raise SystemExit(main())
