#!/usr/bin/env python3
"""Executed checks behind the 2026-10-15 review of five clean-parent flags.

Each case runs in the task's own image after the task's own setup: apply one
change (the task's reference fix, the flagged pass, a sibling pass, or nothing),
add a probe test that encodes only what the prompt states, run it, and
optionally run the task's hidden tests on the same tree. One container at a
time. Writes ``results.json`` next to this file.

  uv run python research/experiments/qa-trace/defect-review/run_probes.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.mimo_clean import SNAPSHOT_REL  # noqa: E402
from evallab.qa.counterfactual import reference_patch, strip_binary_artifacts  # noqa: E402
from evallab.results_home import results_root  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402

TASKS = shared_checkout_root(REPO) / SNAPSHOT_REL / "tasks"
KEY = REPO / "research" / "answer-keys" / "trace-paired-v1" / "items.jsonl"


def _trial_diff(trial_id: str) -> str:
    """The trial's recorded agent.diff without the bytecode stanzas @10 also drops."""
    for line in KEY.read_text().splitlines():
        item = json.loads(line)
        if item["subject"]["trial_id"] == trial_id:
            path = results_root() / item["trial_ref"]["path"] / "verifier" / "agent.diff"
            return strip_binary_artifacts(path.read_text())[0]
    raise KeyError(trial_id)


def _examples(diff: str) -> str:
    return " ".join(re.findall(r"^\+\+\+ b/(recipes/examples/\S+\.py)$", diff, re.M))


PIP_AUDIT = (
    "cd /workspace/repo && PYTHONPATH=/workspace/repo PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
    "python -m pytest usercase-test-coderl/test_qa_probe_alias_exclusion.py -v -p no:cacheprovider"
)
HPCCM = (
    "cd /testbed && { dev-switch python3 3.11 >/dev/null 2>&1; "
    "python3 -m pip install -q -e . --no-input >/dev/null 2>&1; } ; "
    'PROBE_EXAMPLES="$EXAMPLES" python3 -m pytest test/test_qa_probe_include.py -v -p no:cacheprovider'
)
RESPONSES = "cd /testbed && python3 -m pytest responses/tests/test_qa_probe_activate_strict.py -v -p no:cacheprovider"

CASES: list[dict] = [
    {
        "case": "002391 primary id kept out of aliases",
        "task": "format-code-task-002391",
        "probe": "test_qa_probe_alias_exclusion.py",
        "dest": "usercase-test-coderl/test_qa_probe_alias_exclusion.py",
        "command": PIP_AUDIT,
        "changes": {
            "reference fix": lambda: reference_patch("mimo-v2.6-rl/format-code-task-002391", REPO),
            "flagged parent har104-d-002391__WxBjcjX": lambda: _trial_diff(
                "har104-d-002391__WxBjcjX"
            ),
            "child chrr1-l-cheat-002391-k1__QGXLXwa": lambda: _trial_diff(
                "chrr1-l-cheat-002391-k1__QGXLXwa"
            ),
            "no change": lambda: "",
        },
    },
    {
        "case": "000169 included building block reused; examples build",
        "task": "format-code-task-000169",
        "probe": "test_qa_probe_include.py",
        "dest": "test/test_qa_probe_include.py",
        "command": HPCCM,
        "changes": {
            "reference fix": lambda: reference_patch("mimo-v2.6-rl/format-code-task-000169", REPO),
            "flagged parent ovn-g5-000169-gepa__Hmxf7wb": lambda: _trial_diff(
                "ovn-g5-000169-gepa__Hmxf7wb"
            ),
            "child ovn-g5-000169-tuned__CxBpceW": lambda: _trial_diff(
                "ovn-g5-000169-tuned__CxBpceW"
            ),
        },
    },
    {
        "case": "001269 separate strict decorator (prompt option 1) vs hidden tests",
        "task": "format-code-task-001269",
        "probe": "test_qa_probe_activate_strict.py",
        "dest": "responses/tests/test_qa_probe_activate_strict.py",
        "command": RESPONSES,
        "alt": "alt_activate_strict.py",
        "hidden": True,
        "changes": {"alternative design: responses.activate_strict": lambda: ""},
    },
    {
        "case": "001269 reference (oracle) vs hidden tests",
        "task": "format-code-task-001269",
        "hidden": True,
        "changes": {
            "oracle parent 66feb63b1db7__cxLs2vL": lambda: _trial_diff("66feb63b1db7__cxLs2vL")
        },
    },
]


def run_one(case: dict, label: str, change: str, scratch: Path) -> dict:
    task = TASKS / case["task"]
    env = tomllib.loads((task / "task.toml").read_text())["environment"]
    workdir = env["workdir"]
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "change.diff").write_text(change or "")
    mounts = [
        "-v",
        f"{task / 'environment' / 'setup'}:/in/setup:ro",
        "-v",
        f"{task / 'tests'}:/in/tests:ro",
        "-v",
        f"{scratch / 'change.diff'}:/in/change.diff:ro",
        "-v",
        f"{HERE / 'probes'}:/in/probes:ro",
    ]
    steps = [
        "set -e",
        "mkdir -p /var/lib/mimo && cp -r /in/setup/. /var/lib/mimo/ && bash /var/lib/mimo/setup.sh >/dev/null",
        f"cd {workdir}",
        "if [ -s /in/change.diff ]; then git apply --whitespace=nowarn /in/change.diff; fi",
    ]
    if case.get("alt"):
        steps.append(f"python3 /in/probes/{case['alt']}")
    steps.append("set +e")
    if case.get("probe"):
        steps += [
            f"mkdir -p $(dirname {workdir}/{case['dest']}) && cp /in/probes/{case['probe']} {workdir}/{case['dest']}",
            f"export EXAMPLES='{_examples(change or '')}'",
            f"( {case['command']} ) 2>&1 | grep -v Warning | tail -60; echo PROBE_RC=${{PIPESTATUS[0]}}",
        ]
    if case.get("hidden"):
        steps += [
            f"cd {workdir} && git apply --whitespace=nowarn /in/tests/test.patch",
            "( bash /in/tests/test_command.sh ) 2>&1 | tail -15; echo HIDDEN_RC=${PIPESTATUS[0]}",
        ]
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        *mounts,
        "--entrypoint",
        "bash",
        env["docker_image"],
        "-c",
        "\n".join(steps),
    ]
    done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    out = done.stdout + done.stderr
    probe_rc = re.search(r"PROBE_RC=(\d+)", out)
    hidden_rc = re.search(r"HIDDEN_RC=(\d+)", out)
    return {
        "case": case["case"],
        "task": case["task"],
        "change": label,
        "probe_pass": None if not probe_rc else probe_rc.group(1) == "0",
        "hidden_pass": None if not hidden_rc else hidden_rc.group(1) == "0",
        "container_rc": done.returncode,
        "tail": out[-2500:],
    }


def main() -> int:
    rows = []
    for case in CASES:
        for label, change in case["changes"].items():
            row = run_one(
                case, label, change(), Path("/private/tmp/qa-trace-defect-review") / case["task"]
            )
            print(
                f"{row['case']} | {label} | probe={row['probe_pass']} hidden={row['hidden_pass']}",
                flush=True,
            )
            rows.append(row)
    (HERE / "results.json").write_text(json.dumps(rows, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
