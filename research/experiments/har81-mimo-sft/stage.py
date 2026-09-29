"""HAR-81 distill staging: cohorts, prepared specs, queue submission. Nothing is approved here.

    uv run python research/experiments/har81-mimo-sft/stage.py cohort            # ($0) cohort.json
    uv run python research/experiments/har81-mimo-sft/stage.py costs             # ($0) cost table
    uv run python research/experiments/har81-mimo-sft/stage.py prepare pair      # ($0) 40 specs
    uv run python research/experiments/har81-mimo-sft/stage.py prepare heldout   # ($0) distill-only specs
    uv run python research/experiments/har81-mimo-sft/stage.py submit pair       # queue; each spec waits for approval

Batches:
    pair     the distill and its base, Qwen3.5-9B on Tinker, on the same train tasks.
             The two arms share the harness tree and ceilings; only the model differs.
    heldout  the distill alone on every held-out terminal, cyber and code task.

Tasks come from the sealed split minus `tasks catalog export-broken --backend daytona`.
Re-run `cohort` after HAR-88's Daytona qualification lands so its broken_on_daytona
findings drop out. Run from the checkout that will dispatch: prepared specs point at
repo-relative snapshots under runs/.prepared-tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from evallab.execution_contracts import (  # noqa: E402
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    TINKER_MODEL_PRICES_MICROS,
    mimo_selfhosted_trial_cost_usd,
)
from evallab.task_catalog import task_store_root  # noqa: E402
from evallab.task_qualification import estimate_cost_usd  # noqa: E402

EXP = ROOT / "research/experiments/har81-mimo-sft"
HARNESS = EXP / "harness"
IDS_DIR = ROOT / "derived/har81"
COHORT_SALT = "har81-distill"
PAIR_COUNTS = {"terminal": 7, "cyber": 7, "code": 6}
HELDOUT_DOMAINS = ("terminal", "cyber", "code")
#: Queue dispatch width: Tier 2 Daytona fits it, and the server share per trial is
#: the Modal container rate divided by the trials it batches at once.
CONCURRENCY = 16
#: Identical ceilings for both arms. HAR-90's distill trials reached 200 requests or
#: about 2.4M input tokens in 7-10 minutes, so these ceilings, not the task's agent
#: timeout, usually end a looping trial.
MAX_REQUESTS = 200
MAX_INPUT_TOKENS = 2_500_000
MAX_OUTPUT_TOKENS = 131_072
TINKER_BASE = "Qwen/Qwen3.5-9B"
#: Daytona sandbox life beyond the agent timeout: verifier timeout, image pull and
#: setup (300 s), and the provider TTL margin the lifecycle wrapper sets (600 s).
SANDBOX_MARGIN_S = 900
#: Tasks that declare no disk request Daytona's per-sandbox maximum (HAR-88, #499).
DAYTONA_MAX_DISK_MB = 10240
#: Expected-cost assumptions, labelled wherever they are printed.
EXPECTED_TRIAL_HOURS = 10 / 60
EXPECTED_SETUP_HOURS = 5 / 60
WARM_PERIOD_USD = 0.40
ARMS = {
    "d": MIMO_SELFHOSTED_MODEL_SELECTOR,
    "q": f"tinker/{TINKER_BASE}",
}
BATCH_ARMS = {"pair": ("d", "q"), "heldout": ("d",)}


def _tinker_rates() -> tuple[float, float]:
    prefill, sample = TINKER_MODEL_PRICES_MICROS[TINKER_BASE]
    return prefill / 1e12, sample / 1e12


def tinker_token_usd(input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = _tinker_rates()
    return input_tokens * rate_in + output_tokens * rate_out


def cost_limit_usd(arm: str) -> float:
    """Per-trial model ceiling. The distill's tokens are free (the proxy prices them at
    zero), so its nominal $0.01 never trips; Tinker's covers the token ceilings."""
    if arm == "d":
        return 0.01
    return math.ceil(tinker_token_usd(MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS) * 100) / 100


def task_profile(snapshot: Path) -> dict:
    doc = tomllib.loads((snapshot / "task.toml").read_text())
    env, agent, verifier = doc["environment"], doc["agent"], doc["verifier"]
    storage = env.get("storage_mb")
    return {
        "cpus": env["cpus"],
        "memory_mb": env["memory_mb"],
        "storage_mb": storage or DAYTONA_MAX_DISK_MB,
        "override_storage_mb": None if storage else DAYTONA_MAX_DISK_MB,
        "agent_timeout_s": agent["timeout_sec"],
        "verifier_timeout_s": verifier["timeout_sec"],
    }


def sandbox_usd(profile: dict, seconds: float) -> float:
    return estimate_cost_usd(
        backend="daytona",
        sandbox_seconds=seconds,
        cpus=profile["cpus"],
        memory_mb=profile["memory_mb"],
        storage_mb=profile["storage_mb"],
    )


def worst_usd(arm: str, profile: dict) -> float:
    """Spec estimate: the ceiling plus a sandbox that lives to its TTL and, for the
    distill, the server share for the full agent timeout at CONCURRENCY."""
    sandbox_s = profile["agent_timeout_s"] + profile["verifier_timeout_s"] + SANDBOX_MARGIN_S
    total = cost_limit_usd(arm) + sandbox_usd(profile, sandbox_s)
    if arm == "d":
        total += mimo_selfhosted_trial_cost_usd(profile["agent_timeout_s"] / 3600, CONCURRENCY, 0.0)
    return math.ceil(total * 100) / 100


def expected_usd(arm: str, profile: dict, tinker_tokens: tuple[int, int]) -> float:
    total = sandbox_usd(profile, (EXPECTED_TRIAL_HOURS + EXPECTED_SETUP_HOURS) * 3600)
    if arm == "d":
        return total + mimo_selfhosted_trial_cost_usd(EXPECTED_TRIAL_HOURS, CONCURRENCY, 0.0)
    return total + tinker_token_usd(*tinker_tokens)


def load_split() -> dict:
    return json.loads((EXP / "split.json").read_text())


def snapshot_dir(split: dict, task: dict) -> Path:
    repo, rev = split["sources"][task["domain"]].split("@")
    return (
        task_store_root(ROOT)
        / "hf"
        / f"{repo.replace('/', '__')}@{rev[:12]}"
        / "tasks"
        / task["task_id"]
    )


def load_cohort(split: dict) -> dict:
    cohort = json.loads((EXP / "cohort.json").read_text())
    if cohort["split_manifest_digest"] != split["manifest_digest"]:
        raise SystemExit("cohort.json was built on another split; re-run `stage.py cohort`")
    return cohort


def cmd_cohort(_args: argparse.Namespace) -> None:
    split = load_split()
    broken_path = IDS_DIR / "broken-daytona.json"
    IDS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "evallab",
            "tasks",
            "catalog",
            "export-broken",
            "--backend",
            "daytona",
            "--out",
            str(broken_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    broken = json.loads(broken_path.read_text())
    excluded = {item["task_version_digest"] for item in broken["items"]}
    usable = [t for t in split["tasks"] if t["task_version_digest"] not in excluded]

    def rank(task: dict) -> str:
        return hashlib.sha256(f"{COHORT_SALT}\0{task['task_id']}".encode()).hexdigest()

    def row(task: dict) -> dict:
        return {k: task[k] for k in ("domain", "task_id", "split_group", "task_version_digest")}

    per_domain: dict[str, list[dict]] = {}
    for domain, n in PAIR_COUNTS.items():
        groups: set[str] = set()
        picked = per_domain.setdefault(domain, [])
        for task in sorted(
            (t for t in usable if t["split"] == "train" and t["domain"] == domain), key=rank
        ):
            if task["split_group"] not in groups:
                groups.add(task["split_group"])
                picked.append(row(task))
            if len(groups) == n:
                break
    heldout_by_domain = {
        domain: [
            row(t)
            for t in sorted(usable, key=lambda t: t["task_id"])
            if t["split"] == "heldout" and t["domain"] == domain
        ]
        for domain in HELDOUT_DOMAINS
    }
    pair, heldout = round_robin(per_domain), round_robin(heldout_by_domain)
    cohort = {
        "schema": "har81.distill_cohort/v1",
        "split_manifest_digest": split["manifest_digest"],
        "broken_export_sha256": broken["sha256"],
        "excluded_task_version_digests": sorted(excluded),
        "rule": (
            "sealed split minus `tasks catalog export-broken --backend daytona`. pair: train only, "
            f"per domain rank sha256('{COHORT_SALT}\\0'+task_id), one task per split_group, "
            f"{PAIR_COUNTS}. heldout: every held-out task in {list(HELDOUT_DOMAINS)}, by task_id. "
            "Both lists alternate domains so any prefix spans all three."
        ),
        "pair": pair,
        "heldout": heldout,
    }
    (EXP / "cohort.json").write_text(json.dumps(cohort, indent=2) + "\n")
    counts = {d: sum(t["domain"] == d for t in heldout) for d in HELDOUT_DOMAINS}
    print(
        f"pair: {len(pair)} train tasks; heldout: {len(heldout)} {counts}; excluded {len(excluded)}"
    )


def round_robin(per_domain: dict[str, list[dict]]) -> list[dict]:
    """Alternate domains, so a first approval wave (a prefix) covers each of them."""
    longest = max(len(rows) for rows in per_domain.values())
    return [rows[i] for i in range(longest) for rows in per_domain.values() if i < len(rows)]


def spec_name(batch: str, arm: str, task_id: str) -> str:
    return f"har81-{batch[0]}-{arm}-{task_id.replace('_', '-')}".lower()[:80]


def cmd_prepare(args: argparse.Namespace) -> None:
    split = load_split()
    cohort = load_cohort(split)
    out_dir = ROOT / "derived/prepared"
    written = 0
    for task in cohort[args.batch]:
        snapshot = snapshot_dir(split, task)
        profile = task_profile(snapshot)
        for arm in BATCH_ARMS[args.batch]:
            name = spec_name(args.batch, arm, task["task_id"])
            out = out_dir / f"{name}.json"
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--no-sync",
                    "evallab",
                    "tasks",
                    "prepare",
                    str(snapshot),
                    "--name",
                    name,
                    "--agent",
                    "terminus-2",
                    "--model",
                    ARMS[arm],
                    "--environment",
                    "daytona",
                    "--harness-tree",
                    str(HARNESS),
                    "--max-requests",
                    str(MAX_REQUESTS),
                    "--max-input-tokens",
                    str(MAX_INPUT_TOKENS),
                    "--max-output-tokens",
                    str(MAX_OUTPUT_TOKENS),
                    "--cost-limit-usd",
                    f"{cost_limit_usd(arm):.2f}",
                    "--estimated-cost-usd",
                    f"{worst_usd(arm, profile):.2f}",
                    "--output",
                    str(out.relative_to(ROOT)),
                    "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            if profile["override_storage_mb"]:
                spec = json.loads(out.read_text())
                spec["override_storage_mb"] = profile["override_storage_mb"]
                out.write_text(json.dumps(spec, indent=2) + "\n")
            written += 1
    print(f"{args.batch}: {written} specs in derived/prepared/har81-{args.batch[0]}-*.json")


def cmd_submit(args: argparse.Namespace) -> None:
    split = load_split()
    cohort = load_cohort(split)
    ids_path = IDS_DIR / f"{args.batch}.ids"
    IDS_DIR.mkdir(parents=True, exist_ok=True)
    ids = []
    # Interleave arms per task so a pair's two trials share a dispatch window.
    for task in cohort[args.batch]:
        for arm in BATCH_ARMS[args.batch]:
            spec = ROOT / "derived/prepared" / f"{spec_name(args.batch, arm, task['task_id'])}.json"
            out = subprocess.run(
                ["uv", "run", "--no-sync", "evallab", "submit", str(spec.relative_to(ROOT))],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            ids += [
                line.removeprefix("spec_id: ")
                for line in out.splitlines()
                if line.startswith("spec_id: ")
            ]
    ids_path.write_text("\n".join(ids) + "\n")
    rel = ids_path.relative_to(ROOT)
    wave = len(HELDOUT_DOMAINS) * len(BATCH_ARMS[args.batch])
    print(f"{len(ids)} specs waiting for approval -> {rel}")
    print(f"  wave 1, one task per domain ({wave} specs):")
    print(
        f'    for id in $(head -n {wave} {rel}); do uv run evallab approve "$id" --actor peter; done'
    )
    print(f"    uv run evallab tick --parallel {CONCURRENCY}")
    print("  the rest, after comparing measured cost with `stage.py costs`:")
    print(
        f'    for id in $(tail -n +{wave + 1} {rel}); do uv run evallab approve "$id" --actor peter; done'
    )
    print(f"    uv run evallab tick --parallel {CONCURRENCY}")


def cmd_costs(_args: argparse.Namespace) -> None:
    split = load_split()
    cohort = load_cohort(split)
    ceiling_tokens = (MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    har90_tokens = (2_400_000, 13_000)
    light_tokens = (480_000, 20_000)
    print(
        f"Tinker {TINKER_BASE}: cost_limit ${cost_limit_usd('q'):.2f}/trial "
        f"(= {MAX_INPUT_TOKENS:,} in + {MAX_OUTPUT_TOKENS:,} out at list price)"
    )
    print(
        f"assumptions: expected trial {EXPECTED_TRIAL_HOURS * 60:.0f} min + "
        f"{EXPECTED_SETUP_HOURS * 60:.0f} min setup; distill server shared by {CONCURRENCY}; "
        f"+${WARM_PERIOD_USD:.2f} per warm period"
    )
    for batch, arms in BATCH_ARMS.items():
        for arm in arms:
            rows = [task_profile(snapshot_dir(split, t)) for t in cohort[batch]]
            worst = sum(worst_usd(arm, p) for p in rows)
            if arm == "d":
                exp = sum(expected_usd(arm, p, (0, 0)) for p in rows) + WARM_PERIOD_USD
                print(
                    f"{batch:8} {ARMS[arm]:50} n={len(rows):3}  expected ${exp:7.2f}  spec-estimate sum ${worst:7.2f}"
                )
            else:
                light = sum(expected_usd(arm, p, light_tokens) for p in rows)
                har90 = sum(expected_usd(arm, p, har90_tokens) for p in rows)
                ceil = sum(expected_usd(arm, p, ceiling_tokens) for p in rows)
                print(
                    f"{batch:8} {ARMS[arm]:50} n={len(rows):3}  expected ${light:.2f} (0.48M in) / "
                    f"${har90:.2f} (HAR-90 2.4M in) / ${ceil:.2f} (at ceiling)  spec-estimate sum ${worst:.2f}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cohort").set_defaults(func=cmd_cohort)
    sub.add_parser("costs").set_defaults(func=cmd_costs)
    for name, func in (("prepare", cmd_prepare), ("submit", cmd_submit)):
        p = sub.add_parser(name)
        p.add_argument("batch", choices=sorted(BATCH_ARMS))
        p.set_defaults(func=func)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
