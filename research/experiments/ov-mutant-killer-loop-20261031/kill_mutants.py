#!/usr/bin/env python3
"""W2-MUTANT-KILLER: mutation-guided behavioral repair + ``evallab harden`` on 8 weak DEV graders (Modal, cap $1.75).

Closes the STING loop opened by W2-STING-MUTANTS (``ov-sting-mutants-20261031``): for each of the eight tasks whose
grader accepted a semantic mutant of the reference patch, one ``anthropic/claude-fable-5-1`` call writes a behavioral
``unittest`` file exercising the contract the surviving mutant breaks (Round-1 diffs are shown; Round-2 holdout mutants
never are). The file is hooked into ``tests/test.sh`` the way the repaired snapshots already hook ``test_outputs.py``,
and the package is run through ``evallab.full_hardening.harden`` (single-container L1-L3; see README for why not L4).

Modal cells per task (``ModalBackend`` shortcut channel, one sandbox each, consumed by ``result.json``): reference
control, nop control, recorded witness candidates (repaired tasks), every Round-1 survivor, and two Round-2 holdout
mutants on both the upgraded and the base grader. Costs are measured sandbox seconds x Modal rates x 1.25, appended
to ``~/.local/state/evallab-failure-library/spend.jsonl`` as ``w2-mutant-killer:*``.

Usage (from the worktree, ``uv run --no-sync --extra laminar python kill_mutants.py``):
  --synthesize   fable calls (<= 4 concurrent, foreground) + hook + harden -> upgraded packages
  --plan         print the cell plan
  --run          execute the Modal cells (skips consumed cells)
  --summarize    write results/summary.json, cells.csv, table.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.full_hardening import harden  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

DATASET = "mimo-defect-map-v1"
MODEL = "openrouter/~anthropic/claude-sonnet-latest"
FABLE_DIR = Path.home() / "Developer/research-context/bridge/2026-10-31-overnight/or-sonnet"
WORKER_YML = FABLE_DIR / "worker.yml"
RESULTS_HOME = Path.home() / "Developer/eval-lab-results/2026-10-24/mimo-task-quality-30-20261024"
STING_WT = REPO.parent / "ov-w2-sting-mutants-20261031"
STING_RESULTS = STING_WT / "research/experiments/ov-sting-mutants-20261031/results"
STING_CELLS = Path.home() / "Developer/eval-lab-results/2026-11-01/w2-sting-mutants/tasks"
REFERENCES = REPO / "research/experiments/mimo-v6-full-fleet/references.csv"
OUT = Path.home() / "Developer/eval-lab-results/2026-11-01/w2-mutant-killer-r"
RESULTS = HERE / "results-r"
STATE = Path.home() / ".local/state/evallab-failure-library"
LEDGER = STATE / "w2-mutant-killer-r-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
TAG = "w2-mutant-killer-r"
APPROVAL = "Peter 2026-10-31 overnight factory W2-MUTANT-KILLER-R: Modal cap $1.75"
CAP_USD = 1.75
TEST_NAME = "test_mutant_behavior.py"
HOOK_ANCHOR = 'tail -c 12000 "$V/test_output.log"\n'
R2_PER_TASK = 2
MAX_PROMPT_SECTION = 40_000

#: The eight tasks (prereg): kind decides the base package; ``hook`` is the interpreter command for the originals,
#: taken from the decoded hidden ``.build_env/test_command.sh`` / ``mimo_test_command.sh`` (README).
TASKS: dict[str, dict[str, str]] = {
    "000079": {"kind": "repaired"},
    "000232": {"kind": "repaired"},
    "001267": {"kind": "repaired"},
    "001850": {"kind": "repaired"},
    "000682": {"kind": "original", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python3'},
    "000931": {"kind": "original", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python3'},
    "000484": {
        "kind": "original",
        "hook": 'cd "$CWD" && export PATH="$HOME/.pyenv/bin:$PATH" && eval "$(pyenv init -)" '
        "&& MPLBACKEND=Agg PYTHONPATH=/testbed python",
    },
    "003005": {"kind": "original", "hook": 'cd "$CWD" && PYTHONPATH=/testbed python'},
}
FORBIDDEN = ("getsource", "ast.parse", "import ast", "tokenize", "linecache", "__code__")
STRIP_PATCH_FILES = re.compile(r"\.b64$|\.json$|^mimo_test_command\.sh$")


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def load_mutant_auditor():
    """``evallab.mutant_auditor`` from this tree when merged, else the W2-STING-MUTANTS worktree copy (PR #1696)."""
    try:
        import evallab.mutant_auditor as module

        return module, "src/evallab/mutant_auditor.py"
    except ImportError:
        path = STING_WT / "src/evallab/mutant_auditor.py"
        spec = importlib.util.spec_from_file_location("evallab.mutant_auditor", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["evallab.mutant_auditor"] = module
        spec.loader.exec_module(module)
        return module, str(path)


MA, MA_PATH = load_mutant_auditor()


# --------------------------------------------------------------------------- cohort


def task_id(short: str) -> str:
    return f"format-code-task-{short}"


def repaired_snapshot(short: str) -> Path:
    root = RESULTS_HOME / "evidence/packages" / task_id(short)
    snapshots = sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file())
    return snapshots[-1]


def cohort() -> dict[str, dict[str, Any]]:
    registry = CorpusRegistry(repo_root=REPO)
    selection = json.loads((STING_RESULTS / "selection.json").read_text())
    chosen = {t["task_id"]: t for t in selection["chosen"]}
    audit = json.loads((STING_RESULTS / "audit-summary.json").read_text())
    records = {r["task_id"]: r for r in audit["tasks"]}
    references = {r["task_id"]: r for r in csv.DictReader(REFERENCES.open())}
    out: dict[str, dict[str, Any]] = {}
    for short, spec in TASKS.items():
        tid = task_id(short)
        if registry.assigned_split(DATASET, task_id=tid) != "dev" or registry.assigned_test_anywhere(
            task_id=tid
        ):
            raise SystemExit(f"{tid} is not strictly DEV")
        patch = (REPO / references[tid]["patch_path"]).read_bytes()
        if sha256(patch) != references[tid]["patch_sha256"]:
            raise SystemExit(f"{tid}: reference patch sha256 mismatch")
        record = records[tid]
        grader = "repaired" if spec["kind"] == "repaired" else "original"
        survivors = record[f"survivors_{grader}"]
        if not survivors:
            raise SystemExit(f"{tid}: no Round-1 survivor on the {grader} grader")
        rows = {r["cell"]: r for r in record[grader]}
        base = repaired_snapshot(short) if grader == "repaired" else Path(chosen[tid]["package"])
        out[short] = {
            "task_id": tid,
            "kind": spec["kind"],
            "base_package": base,
            "patch": patch.decode(),
            "patch_sha256": references[tid]["patch_sha256"],
            "r1_mutants": [rows[c]["mutant"] for c in survivors],
            "r1_all": record["mutants"],
            "witnesses": sorted(
                (RESULTS_HOME / "evidence/candidates" / tid).glob("*/solve.sh")
            )
            if grader == "repaired"
            else [],
        }
    return out


def probe_sources(short: str) -> dict[str, str]:
    result = json.loads((STING_CELLS / task_id(short) / "probe/result.json").read_text())
    return MA.parse_probe(result["agent_log"])


def r1_diff(short: str, mutant: dict[str, Any]) -> str:
    return (
        STING_RESULTS / "survivor-diffs" / f"{task_id(short)}-{mutant['operator']}-L{mutant['line']}.diff"
    ).read_text()


# --------------------------------------------------------------------------- round-2 holdout mutants


def round2_mutants(short: str, task: dict[str, Any]) -> list[Any]:
    """Two unseen mutants: lines outside every Round-1 mutant line, operators unlike the survivors' when possible."""
    sources = probe_sources(short)
    targets = MA.python_targets(task["patch"])
    seen_lines = {(m["path"], m["line"]) for m in task["r1_all"]}
    survivor_ops = {m["operator"] for m in task["r1_mutants"]}
    pool: list[Any] = []
    for path in sorted(targets):
        if path not in sources:
            continue
        for mutant in MA.generate_mutants(path, sources[path], targets[path], limit=16):
            if (mutant.path, mutant.line) not in seen_lines:
                pool.append(mutant)
    preferred = [m for m in pool if m.operator not in survivor_ops]
    chosen = (preferred + [m for m in pool if m not in preferred])[:R2_PER_TASK]
    return chosen


# --------------------------------------------------------------------------- synthesis


def patch_sections(patch: str, keep) -> str:
    """The unified-diff sections of ``patch`` whose file path satisfies ``keep``."""
    out: list[str] = []
    capture = False
    for line in patch.splitlines(keepends=True):
        header = re.match(r"diff --git a/(\S+) b/", line)
        if header:
            capture = keep(header.group(1))
        if capture:
            out.append(line)
    text = "".join(out)
    return text if len(text) <= MAX_PROMPT_SECTION else text[:MAX_PROMPT_SECTION] + "\n[... truncated]\n"


def synthesis_prompt(short: str, task: dict[str, Any], interpreter: str) -> str:
    base = task["base_package"]
    hidden = patch_sections(
        (base / "tests/test.patch").read_text(errors="replace"),
        lambda p: not STRIP_PATCH_FILES.search(p),
    )
    reference = patch_sections(task["patch"], lambda p: p.endswith(".py"))
    existing = base / "tests/test_outputs.py"
    diffs = "\n".join(r1_diff(short, m) for m in task["r1_mutants"])
    parts = [
        "You are writing hidden behavioral unit tests for a software task grader (the STING mutation-testing repair "
        "loop). A semantic mutant of the reference implementation currently passes the grader; your tests must "
        "fail on the mutant and pass on the correct implementation.\n",
        "## Task instruction (what the agent was asked to implement)\n",
        (base / "instruction.md").read_text(errors="replace"),
        "\n## Hidden tests already in the grader (unified diff, test code only)\n",
        hidden,
        "\n## Reference implementation (unified diff of the Python sources, the correct behavior)\n",
        reference,
    ]
    if existing.is_file():
        parts += [
            "\n## Behavioral test file already appended by an earlier repair (keep its style; do not duplicate it)\n",
            existing.read_text(errors="replace"),
        ]
    parts += [
        "\n## Surviving semantic mutant(s) of the reference implementation that the grader wrongly ACCEPTS\n",
        diffs,
        "\n## Your job\n",
        f"Write ONE Python `unittest` file. It will be saved as /tests/{TEST_NAME} and run as\n"
        f"`cd /testbed && {interpreter} /tests/{TEST_NAME}` after the existing hidden tests pass "
        "(the repository is at /testbed; add `sys.path.insert(0, '/testbed')` first). The file must:\n"
        "- exercise, through the PUBLIC API of the package, the behavior each mutant breaks, as general contract "
        "tests (not a copy of the mutant diff): pass on the reference implementation, fail on every mutant shown;\n"
        "- NEVER inspect source text: no inspect.getsource, no ast, no tokenize, no reading .py files, no regex over "
        "source. Only observable behavior counts;\n"
        "- use only the dependencies the existing hidden tests use; no network; deterministic; finish in < 60 s;\n"
        f"- be compatible with {'Python 2.7' if short in ('000484', '001267') else 'Python 3'}; "
        "end with `if __name__ == '__main__': unittest.main()`;\n"
        "- keep setup minimal; prefer 2-5 focused test methods; do not mock the code under test.\n"
        "You have no tools and no repository access: write the file from the material above only and never emit "
        "tool calls. Output exactly one fenced ```python block with the complete file and nothing else.\n",
    ]
    return "".join(parts)


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.S)
    return blocks[-1] if blocks else None


def served_model(session_dir: Path) -> tuple[str | None, float]:
    """``provider/model`` of the assistant message in the session log and the recorded cost."""
    model, cost = None, 0.0
    for log in session_dir.glob("*.jsonl"):
        for line in log.read_text().splitlines():
            row = json.loads(line)
            message = row.get("message") or {}
            if row.get("type") == "message" and message.get("role") == "assistant":
                model = f"{message.get('provider')}/{message.get('model')}"
                cost += float((message.get("usage") or {}).get("cost", {}).get("total", 0.0))
    return model, cost


def synthesize_one(short: str, task: dict[str, Any], interpreter: str) -> dict[str, Any]:
    session_dir = OUT / "sessions" / short
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = synthesis_prompt(short, task, interpreter)
    feedback = RESULTS / "feedback" / f"{short}.md"
    if feedback.is_file():  # disclosed one-shot revision round after a reference false reject
        prompt += feedback.read_text()
    (RESULTS / "prompts").mkdir(parents=True, exist_ok=True)
    (RESULTS / "prompts" / f"{short}.md").write_text(prompt)
    started = time.monotonic()
    proc = subprocess.run(
        [
            "omp",
            "-p",
            "--model",
            MODEL,
            "--config",
            str(WORKER_YML),
            "--no-tools",
            "--no-skills",
            "--session-dir",
            str(session_dir),
            prompt,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=900,
        cwd="/tmp",
        env={**os.environ, "OMP_NO_COLOR": "1"},
    )
    model, cost = served_model(session_dir)
    code = extract_code(proc.stdout)
    status = "ok"
    if proc.returncode != 0 or code is None:
        status = f"failed (rc={proc.returncode}, block={'yes' if code else 'no'})"
    elif not (model and model.startswith("openrouter/") and "claude-sonnet" in model) or "argon" in model:
        status = f"rejected: served {model}"
    elif any(word in code for word in FORBIDDEN) or "unittest" not in code:
        status = "refused: source inspection or no unittest"
    record = {
        "task": short,
        "status": status,
        "served_model": model,
        "attempts": len(list(session_dir.glob("*.jsonl"))),
        "cost_usd": round(cost, 4),
        "wall_s": round(time.monotonic() - started, 1),
        "prompt_sha256": sha256(prompt),
        "test_sha256": sha256(code) if code else None,
        "test_lines": len(code.splitlines()) if code else 0,
        "stderr_tail": proc.stderr[-500:],
    }
    if code is not None:
        (RESULTS / "tests").mkdir(parents=True, exist_ok=True)
        (RESULTS / "tests" / f"{short}-{TEST_NAME}.txt").write_text(code)
    return record


# --------------------------------------------------------------------------- hook + harden


def hook_command(short: str, task: dict[str, Any]) -> str:
    """The interpreter prefix for the behavioral hook: the repaired snapshot's own, else the decoded hidden command's."""
    if task["kind"] == "original":
        return TASKS[short]["hook"]
    text = (task["base_package"] / "tests/test.sh").read_text()
    match = re.search(r"\( (cd \"\$CWD\".*?) /tests/test_outputs\.py \)", text)
    if not match:
        raise SystemExit(f"{short}: repaired snapshot hook line not found")
    return match.group(1)


def upgraded_package(short: str, task: dict[str, Any], code: str) -> tuple[Path, dict[str, Any]]:
    stage = OUT / "stage" / short
    target = OUT / "packages" / short
    for path in (stage, target):
        if path.exists():
            shutil.rmtree(path)
    Path(f"{target}.hardening-manifest.json").unlink(missing_ok=True)
    shutil.copytree(task["base_package"], stage, symlinks=True)
    for path in (stage, *stage.rglob("*")):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
    (stage / "tests" / TEST_NAME).write_text(code)
    test_sh = stage / "tests/test.sh"
    text = test_sh.read_text()
    if text.count(HOOK_ANCHOR) != 1:
        raise SystemExit(f"{short}: tests/test.sh hook anchor not found exactly once")
    block = (
        f'if [ "$RC" -eq 0 ]; then\n'
        f"  ( {hook_command(short, task)} /tests/{TEST_NAME} ) >> \"$V/test_output.log\" 2>&1\n"
        f"  RC=$?\nfi\n"
    )
    test_sh.write_text(text.replace(HOOK_ANCHOR, block + HOOK_ANCHOR, 1))
    manifest = harden(stage, target, single_container=True, allow_degraded=True)
    (RESULTS / "manifests").mkdir(parents=True, exist_ok=True)
    (RESULTS / "manifests" / f"{short}.json").write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n"
    )
    return target, {
        "hook": block,
        "hardened_layers": {layer.layer: layer.status for layer in manifest.layers},
        "runner": manifest.runner,
        "package_digest": manifest.output_digest,
    }


# --------------------------------------------------------------------------- cells


def nop_script(workdir: str) -> str:
    return f"set -u\ncd {workdir} || exit 1\necho NOP_APPLIED\n"


def witness_script(solve_sh: Path) -> str:
    return solve_sh.read_text() + f"\necho WITNESS_APPLIED {solve_sh.parent.name}\n"


def workdir_of(package: Path) -> str:
    return tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]


def plan_cells(short: str, task: dict[str, Any], upgraded: Path, r2: list[Any]) -> list[dict[str, Any]]:
    patch = task["patch"]
    cells: list[dict[str, Any]] = []

    def add(name: str, package: Path, script: str, marker: str, must: str, mutant=None) -> None:
        cells.append(
            {
                "task": short,
                "cell": name,
                "package": str(package),
                "script": script,
                "marker": marker,
                "must": must,
                "mutant": None if mutant is None else mutant.summary(),
            }
        )

    wd = workdir_of(upgraded)
    add(
        "reference",
        upgraded,
        MA.probe_script(workdir=wd, reference_patch=patch, targets=[]),
        MA.PROBE_DONE_MARKER,
        "1.0",
    )
    add("nop", upgraded, nop_script(wd), "NOP_APPLIED", "0.0")
    for solve in task["witnesses"]:
        add(f"witness-{solve.parent.name}", upgraded, witness_script(solve), "WITNESS_APPLIED", "0.0")
    sources = probe_sources(short)
    recorded = MA.mutants_for_patch(patch, sources)
    for summary in task["r1_mutants"]:
        mutant = next(
            m
            for m in recorded
            if m.path == summary["path"]
            and m.operator == summary["operator"]
            and m.line == summary["line"]
        )
        if mutant.sha256 != summary["sha256"]:
            raise SystemExit(f"{short}: regenerated Round-1 mutant differs from the recorded one")
        add(
            f"r1-{mutant.operator}-L{mutant.line}",
            upgraded,
            MA.mutant_script(workdir=wd, reference_patch=patch, mutant=mutant),
            f"{MA.APPLIED_MARKER} {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}",
            "0.0",
            mutant,
        )
    for mutant in r2:
        marker = f"{MA.APPLIED_MARKER} {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}"
        add(
            f"r2-{mutant.operator}-L{mutant.line}",
            upgraded,
            MA.mutant_script(workdir=wd, reference_patch=patch, mutant=mutant),
            marker,
            "measured",
            mutant,
        )
        add(
            f"r2base-{mutant.operator}-L{mutant.line}",
            task["base_package"],
            MA.mutant_script(
                workdir=workdir_of(task["base_package"]), reference_patch=patch, mutant=mutant
            ),
            marker,
            "measured",
            mutant,
        )
    return cells


def spend_logging_runner(inner, lock: threading.Lock):
    def run(cell_dir: Path, package: Path, script: str) -> dict:
        fresh = not (cell_dir / "result.json").exists()
        result = inner(cell_dir, package, script)
        if fresh:
            record = {
                "cost_usd": result["est_usd"],
                "job": f"{TAG}:{cell_dir.parent.name}/{cell_dir.name}",
                "note": (
                    f"W2-MUTANT-KILLER cell {cell_dir.parent.name}/{cell_dir.name} "
                    f"status={result['status']} reward={result['reward']} wall_s={result['wall_s']} "
                    "(Modal rates x1.25 estimate)"
                ),
                "route": "modal",
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with lock, SPEND_LOG.open("a") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return result

    return run


# --------------------------------------------------------------------------- summary


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    import math

    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return "0/0" if n == 0 else f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"


def summarize(plan: list[dict[str, Any]], synthesis: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for cell in plan:
        path = OUT / "cells" / cell["task"] / cell["cell"] / "result.json"
        result = json.loads(path.read_text()) if path.exists() else {}
        rows.append(
            {
                "task": cell["task"],
                "cell": cell["cell"],
                "must": cell["must"],
                "applied": cell["marker"] in result.get("agent_log", ""),
                "reward": result.get("reward"),
                "status": result.get("status"),
                "wall_s": result.get("wall_s"),
                "est_usd": result.get("est_usd"),
                "evidence_sha256": result.get("evidence_sha256"),
                "package": cell["package"],
            }
        )
    with (RESULTS / "cells.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    per_task: dict[str, dict[str, Any]] = {}
    for short in TASKS:
        mine = [r for r in rows if r["task"] == short]
        get = {r["cell"]: r for r in mine}
        ok = lambda r: r["applied"] and r["reward"] == 0.0  # noqa: E731
        reference = get.get("reference", {})
        nop = get.get("nop", {})
        valid = bool(reference.get("applied")) and reference.get("reward") == 1.0 and ok(nop)
        r1 = [r for r in mine if r["cell"].startswith("r1-")]
        wit = [r for r in mine if r["cell"].startswith("witness-")]
        r2 = {r["cell"][3:]: r for r in mine if r["cell"].startswith("r2-")}
        r2base = {r["cell"][7:]: r for r in mine if r["cell"].startswith("r2base-")}
        r2_rows = []
        for key, up in r2.items():
            base = r2base.get(key, {})
            r2_rows.append(
                {
                    "mutant": key,
                    "base_reward": base.get("reward"),
                    "upgraded_reward": up.get("reward"),
                    "applied": bool(up.get("applied") and base.get("applied")),
                    "base_survived": bool(base.get("applied")) and base.get("reward") == 1.0,
                    "killed_upgraded": ok(up),
                }
            )
        per_task[short] = {
            "kind": TASKS[short]["kind"],
            "synthesis": synthesis.get(short, {}).get("status"),
            "valid": valid,
            "reference_reward": reference.get("reward"),
            "nop_reward": nop.get("reward"),
            "witness_rewards": {r["cell"]: r["reward"] for r in wit},
            "witness_ok": all(ok(r) for r in wit) if wit else None,
            "r1": {r["cell"]: r["reward"] for r in r1},
            "r1_killed": sum(ok(r) for r in r1),
            "r1_total": len(r1),
            "r1_closed": valid and bool(r1) and all(ok(r) for r in r1),
            "r2": r2_rows,
            "test_lines": synthesis.get(short, {}).get("test_lines"),
        }
    valid_tasks = [t for t, v in per_task.items() if v["valid"]]
    r1_cells = [(t, r) for t in valid_tasks for r in per_task[t]["r1"]]
    r1_killed = sum(per_task[t]["r1_killed"] for t in valid_tasks)
    r2_applied = [r for t in valid_tasks for r in per_task[t]["r2"] if r["applied"]]
    r2_fresh = [r for r in r2_applied if r["base_survived"]]
    wit_cells = [r for t in valid_tasks for r in per_task[t]["witness_rewards"].values()]
    totals = {
        "g1_valid_tasks": fmt(len(valid_tasks), len(per_task)),
        "g2_tasks_closed": fmt(sum(per_task[t]["r1_closed"] for t in valid_tasks), len(valid_tasks)),
        "g2_r1_mutants_killed": fmt(r1_killed, len(r1_cells)),
        "g3_witness_cells_rejected": fmt(
            sum(1 for t in valid_tasks for r in per_task[t]["witness_rewards"].values() if r == 0.0),
            len(wit_cells),
        ),
        "g4_r2_killed_among_base_survivors": fmt(
            sum(r["killed_upgraded"] for r in r2_fresh), len(r2_fresh)
        ),
        "g4_r2_killed_among_applied": fmt(
            sum(r["killed_upgraded"] for r in r2_applied), len(r2_applied)
        ),
        "r2_base_survival": fmt(len(r2_fresh), len(r2_applied)),
        "estimated_usd": round(sum(r["est_usd"] or 0 for r in rows), 4),
        "sandbox_seconds": round(sum(r["wall_s"] or 0 for r in rows), 1),
        "executions": sum(1 for r in rows if r["status"]),
        "synthesis_usd": round(sum(v.get("cost_usd", 0) for v in synthesis.values()), 4),
        "synthesis_attested": fmt(
            sum(1 for v in synthesis.values() if v.get("served_model") == MODEL), len(synthesis)
        ),
    }
    summary = {
        "approval": APPROVAL,
        "cap_usd": CAP_USD,
        "mutant_auditor": MA_PATH,
        "totals": totals,
        "tasks": per_task,
    }
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    lines = [
        "| task | kind | valid (ref/nop) | witnesses | R1 killed | R2 holdout (base -> upgraded) | test lines |",
        "|---|---|---|---|---|---|---|",
    ]
    for short, v in per_task.items():
        r2 = "; ".join(
            f"{r['mutant']} {r['base_reward']}->{r['upgraded_reward']}" for r in v["r2"]
        )
        wit = ", ".join(f"{k.split('-', 1)[1]}={val}" for k, val in v["witness_rewards"].items()) or "-"
        lines.append(
            f"| {short} | {v['kind']} | {v['valid']} ({v['reference_reward']}/{v['nop_reward']}) | {wit} | "
            f"{v['r1_killed']}/{v['r1_total']} | {r2} | {v['test_lines']} |"
        )
    lines += ["", *(f"- {k}: {val}" for k, val in totals.items())]
    (RESULTS / "table.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return summary


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthesize", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--cell-workers", type=int, default=10)
    args = parser.parse_args()
    tasks = cohort()
    if args.only:
        tasks = {k: v for k, v in tasks.items() if k in set(args.only)}
    RESULTS.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    synthesis_path = RESULTS / "synthesis.json"
    synthesis = json.loads(synthesis_path.read_text()) if synthesis_path.exists() else {}

    if args.synthesize:
        with ThreadPoolExecutor(max_workers=4) as pool:  # machine-safety limit on omp -p
            futures = {
                short: pool.submit(synthesize_one, short, task, hook_command(short, task))
                for short, task in tasks.items()
                if synthesis.get(short, {}).get("status") != "ok"
            }
            for short, future in futures.items():
                synthesis[short] = future.result()
                print(short, synthesis[short]["status"], synthesis[short]["served_model"], flush=True)
        synthesis_path.write_text(json.dumps(synthesis, indent=2) + "\n")
        for short, task in tasks.items():
            if synthesis.get(short, {}).get("status") != "ok":
                continue
            code = (RESULTS / "tests" / f"{short}-{TEST_NAME}.txt").read_text()
            _, info = upgraded_package(short, task, code)
            synthesis[short].update(info)
            print(short, "hardened", info["hardened_layers"], flush=True)
        synthesis_path.write_text(json.dumps(synthesis, indent=2) + "\n")

    plan: list[dict[str, Any]] = []
    r2_summary: dict[str, list[dict[str, Any]]] = {}
    for short, task in tasks.items():
        if synthesis.get(short, {}).get("status") != "ok":
            print(short, "skipped: synthesis", synthesis.get(short, {}).get("status"))
            continue
        r2 = round2_mutants(short, task)
        r2_summary[short] = [m.summary() for m in r2]
        (RESULTS / "r2-diffs").mkdir(parents=True, exist_ok=True)
        for mutant in r2:
            (RESULTS / "r2-diffs" / f"{short}-{mutant.operator}-L{mutant.line}.diff").write_text(
                mutant.diff
            )
        plan += plan_cells(short, task, OUT / "packages" / short, r2)
    (RESULTS / "round2-mutants.json").write_text(json.dumps(r2_summary, indent=2) + "\n")
    (RESULTS / "plan.json").write_text(
        json.dumps([{k: v for k, v in c.items() if k != "script"} for c in plan], indent=1) + "\n"
    )
    print(f"plan: {len(plan)} cells over {len(r2_summary)} tasks", flush=True)
    if args.plan:
        for cell in plan:
            print(cell["task"], cell["cell"], cell["must"])

    if args.run:
        from evallab.regrade import CloudSpendGate

        gate = CloudSpendGate(
            approval=APPROVAL,
            cap_usd=CAP_USD,
            usd_per_execution=0.005,
            spend_ledger=LEDGER,
            modal_app_name=TAG,
            billing_tag=TAG,
        )
        meter = MA.SandboxMeter(CAP_USD)
        run_cell = spend_logging_runner(
            MA.modal_cell_runner(repo_root=REPO, spend_gate=gate, meter=meter), threading.Lock()
        )

        def one(cell: dict[str, Any]) -> None:
            cell_dir = OUT / "cells" / cell["task"] / cell["cell"]
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "agent-script.sh").write_text(cell["script"])
            try:
                result = run_cell(cell_dir, Path(cell["package"]), cell["script"])
                print(
                    cell["task"],
                    cell["cell"],
                    "reward=",
                    result["reward"],
                    "applied=",
                    cell["marker"] in result.get("agent_log", ""),
                    f"${result['est_usd']}",
                    flush=True,
                )
            except Exception as exc:  # recorded, never silently dropped
                print(cell["task"], cell["cell"], f"error: {type(exc).__name__}: {exc}", flush=True)

        with ThreadPoolExecutor(max_workers=args.cell_workers) as pool:
            list(pool.map(one, plan))
        print(json.dumps({"estimated_usd": round(meter.spent, 4), "executions": meter.cells}))

    if args.summarize or args.run:
        summarize(plan, synthesis)


if __name__ == "__main__":
    main()
