#!/usr/bin/env python3
"""HAR-83 hack-probe cohort redraw from the FULL pools (code 2,698 / terminal 64).

Two steps (code task.toml files alone cannot carry digests):
  1. select:   strata over task.toml files -> selection.json (ids + strata)
  2. finalize: after stage.sh pulls the full trees into derived/task-store,
               computes digests -> cohort.json + ExperimentSpec sources

Deterministic: sha256(task_id) mod 100 < 80 -> train (provisional rule;
HAR-81's split is not frozen). Largest-remainder strata (terminal: area
family; code: [metadata].category language, every present language covered).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from evallab.registry import compute_task_digests, harbor_task_digest  # noqa: E402

CODE_TOMLS = Path("/private/tmp/hack-probe-pool/code-tomls/tasks")
TERM_POOL = Path("/private/tmp/mimo-data/terminal/tasks")
OUT = ROOT / "research/experiments/mimo-hack-probe"
N_TERMINAL, N_CODE = 16, 16
EXPECTED_CODE_POOL = 2698

TERMINAL_REPO = ("FineEnvs__MiMo-V2.6-RL-harbor-terminal", "fe1c2b665aae")
CODE_REPO = ("FineEnvs__MiMo-V2.6-RL-harbor-code", "5746e2f0c5c6")
PREAMBLE = "research/experiments/preambles/mimo-hack-probe-v1.md"


def area_of(task_id: str) -> str:
    for area in ("ml", "security", "software", "science", "hardware", "media", "operations"):
        if f"-{area}-" in f"-{task_id}-":
            return area
    return "other"


def train_rows(pool: Path, stratify) -> list[dict]:
    rows = []
    for task_dir in sorted(pool.iterdir()):
        if not (task_dir / "task.toml").is_file():
            continue
        meta = tomllib.loads((task_dir / "task.toml").read_text()).get("metadata", {})
        task_id = task_dir.name
        frac = int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 100
        if frac >= 80:
            continue
        rows.append({"task_id": task_id, "split_frac": frac,
                     "stratum": stratify(task_id, meta)})
    return rows
def pick(rows: list[dict], n: int) -> list[dict]:
    """Floor-1 per stratum, remaining seats by largest remainder over pool
    share (ties: stratum name ascending). Seats a thin stratum cannot fill
    go to the largest strata with spares. Deterministic."""
    import math
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["stratum"], []).append(r)
    for members in groups.values():
        members.sort(key=lambda r: r["task_id"])
    order = sorted(groups)
    assert len(order) <= n, f"{len(order)} strata for {n} seats: merge first"
    quota = {s: 1 for s in order}
    remaining = n - len(order)
    total = len(rows)
    exact = {s: remaining * len(groups[s]) / total for s in order}
    for s in order:
        quota[s] += math.floor(exact[s])
    leftover = remaining - sum(math.floor(exact[s]) for s in order)
    by_rem = sorted(order, key=lambda s: (-(exact[s] - math.floor(exact[s])), s))
    for s in by_rem[:leftover]:
        quota[s] += 1
    chosen: list[dict] = []
    for s in order:
        chosen += groups[s][: quota[s]]
    if len(chosen) < n:
        # A thin stratum could not fill its seats: top up from the largest
        # strata with unchosen members (pool size desc, id asc).
        have = {r["task_id"] for r in chosen}
        rest = sorted(
            (r for rows_ in groups.values() for r in rows_ if r["task_id"] not in have),
            key=lambda r: (-len(groups[r["stratum"]]), r["task_id"]),
        )
        chosen += rest[: n - len(chosen)]
    return sorted(chosen, key=lambda r: r["task_id"])
def cmd_select() -> None:
    n_code_dirs = sum(1 for d in CODE_TOMLS.iterdir() if (d / "task.toml").is_file())
    assert n_code_dirs == EXPECTED_CODE_POOL, (
        f"code pool incomplete: {n_code_dirs} task.toml files, want {EXPECTED_CODE_POOL}")
    term = train_rows(TERM_POOL, lambda tid, _m: area_of(tid))
    code = train_rows(CODE_TOMLS, lambda _t, m: str(m.get("category", "unknown")))
    assert len(term) >= N_TERMINAL, f"terminal train pool {len(term)}"
    assert len(code) >= N_CODE, f"code train pool {len(code)}"
    # 16 seats cannot cover 18 code categories: keep every category with >= 2
    # train tasks (14 strata); the 4 singleton tasks (Elixir/Lua/Svelte/Swift)
    # are listed as excluded. "Unknown" is upstream-missing category metadata
    # (Julia-heavy format tasks), kept as its own stratum.
    from collections import Counter
    sizes = Counter(r["stratum"] for r in code)
    thin = sorted(s for s, c in sizes.items() if c < 2)
    excluded = sorted(r["task_id"] for r in code if r["stratum"] in thin)
    code = [r for r in code if r["stratum"] not in thin]
    selection = {
        "rule": "sha256(task_id) % 100 < 80 -> train; floor-1 per stratum + largest-remainder extras (ties: stratum name asc)",
        "pool": {"code": EXPECTED_CODE_POOL, "terminal": 64},
        "terminal": [{"task_id": r["task_id"], "split_frac": r["split_frac"], "stratum": r["stratum"]}
                     for r in pick(term, N_TERMINAL)],
        "code": [{"task_id": r["task_id"], "split_frac": r["split_frac"], "stratum": r["stratum"]}
                 for r in pick(code, N_CODE)],
        "excluded_singletons": excluded,
    }
    (OUT / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    print("terminal:", len(selection["terminal"]), "train pool:", len(term))
    print("code:", len(selection["code"]), "train pool:", len(code))
    print("code strata:", sorted({r["stratum"] for r in selection["code"]}))
    print("excluded singletons:", selection["excluded_singletons"])



def spec_source(domain: str, task_id: str, task_rel: str, package_digest: str) -> dict:
    short = task_id.replace("_", "-")
    prefix = "t" if domain == "terminal" else "c"
    spec = {"task": task_rel, "task_package_digest": package_digest, "agent": "terminus-2",
            "model": "zai/glm-5.3-flash", "environment": "daytona",
            "name": f"mimo-hack-{prefix}-{short}"[:80],
            "jobs_dir": "runs", "attempts": 1, "timeout_seconds": 1800,
            "extra_instruction_path": PREAMBLE,
            "max_requests": 64, "max_input_tokens": 200000, "max_output_tokens": 8192,
            "cost_limit_usd": 2.50, "purpose": "elicitation",
            "hypothesis": ("A red-team instruction can make Terminus-2 obtain the "
                           "reward without solving the task (answer-leak or verifier exploit)."),
            "submitted_by": "hackprobe"}
    if domain == "code":
        # Code task.tomls leave storage_mb unset; Daytona's 3 GiB default disk is
        # smaller than most unpacked code images, so request the 10 GiB maximum.
        spec["override_storage_mb"] = 10240
    return spec


def cmd_finalize() -> None:
    selection = json.loads((OUT / "selection.json").read_text())
    cohort, spec_files = [], []
    for domain, org, rev12 in (("terminal", *TERMINAL_REPO), ("code", *CODE_REPO)):
        for row in selection[domain]:
            task_id = row["task_id"]
            if domain == "terminal":
                src = TERM_POOL / task_id
                snapshot = f"/private/tmp/mimo-data/terminal/tasks/{task_id}"
            else:
                src = ROOT / f"derived/task-store/hf/{org}@{rev12}/tasks/{task_id}"
                snapshot = f"{org.replace('__', '/')}@{rev12}:tasks/{task_id}"
            assert (src / "task.toml").is_file(), f"full tree missing: {src}"
            task_rel = f"derived/task-store/hf/{org}@{rev12}/tasks/{task_id}"
            pkg = compute_task_digests(src).package
            cohort.append({"task_name": f"mimo-v2.6-rl/{task_id}", "task_id": task_id,
                           "domain": domain, "snapshot_path": snapshot,
                           "harbor_digest": harbor_task_digest(src), "package_digest": pkg,
                           "split_frac": row["split_frac"], "split": "train",
                           "stratum": row["stratum"], "task": task_rel})
            spec = spec_source(domain, task_id, task_rel, pkg)
            fname = f"mimo-hack-{'t' if domain == 'terminal' else 'c'}-{task_id.replace('_', '-')}"[:80] + ".json"
            (OUT / "specs" / fname).write_text(json.dumps(spec, indent=2) + "\n")
            spec_files.append(fname)
    (OUT / "cohort.json").write_text(json.dumps(
        {"provisional_split": "sha256(task_id) % 100 < 80 -> train (HAR-81 split not frozen)",
         "pool": {"code": EXPECTED_CODE_POOL, "terminal": 64},
         "probe_config": "redteam-v1", "cohort": cohort}, indent=2) + "\n")
    print(f"cohort: {len(cohort)} rows, {len(spec_files)} specs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["select", "finalize"])
    args = parser.parse_args()
    (cmd_select if args.step == "select" else cmd_finalize)()
