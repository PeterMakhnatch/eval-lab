"""Prepare the exact cheater-recall-v1 cohort; never launch, approve, or spend.

Default: print a read-only plan. --prepare rebuilds missing cached variants from
committed lineage, freezes task copies, and writes native draft specs/campaigns.
No network, Docker, credentials, dataset downloads or model inference are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.campaign_approval import ExperimentCampaign, intended_sampling
from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_MODEL_PREFIX,
    BREAKER_SELFHOSTED_SAMPLING,
    parse_breaker_selfhosted_model,
)
from evallab.exploit_probe import HF_SOURCE, PREAMBLE, SNAPSHOT
from evallab.registry import compute_task_digests
from evallab.schemas import ExperimentSpec
from evallab.storage.paths import shared_checkout_root
from evallab.task_import import import_task_package
from evallab.task_prepare import PREPARED_TASKS_REL
from evallab.task_variants import lineage_chain, materialize, resolve_record
from evallab.terminus_harness import load_harness_tree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE = ROOT / "research/experiments/cheater-recall-v1"
EXPERIMENT = "trained-breaker-v1"
COHORTS = {"leaky": 5, "hardened": 7}
HARNESS = HERE / "harness"
DEVIATIONS = [
    {
        "field": "harness.id",
        "value": "terminus-2",
        "reason": "Compare terminal audit policies, not the Xiaomi training harness.",
    },
    {
        "field": "harness.additions",
        "value": {"extra_instruction": PREAMBLE},
        "reason": "Both arms receive the same explicit authorized-audit instruction.",
    },
    {
        "field": "server.tool_call_parser",
        "value": "terminus-json",
        "reason": "Both Qwen arms use stock Terminus JSON, not the MiMo native-tool normalizer.",
    },
    {
        "field": "server.context_length",
        "value": 65536,
        "reason": "Match the released L1 card's served context for both arms.",
    },
    {
        "field": "sampling.top_p",
        "value": 1.0,
        "reason": "Matched full-nucleus sampling for both arms, not the Xiaomi RL recipe.",
    },
    {
        "field": "sampling.top_k",
        "value": -1,
        "reason": "Disable top-k truncation equally for both arms.",
    },
    {
        "field": "budgets.step_limit",
        "value": 128,
        "reason": "Bound the diagnostic to 128 agent turns for both arms.",
    },
    {
        "field": "budgets.max_requests",
        "value": 128,
        "reason": "Bound both arms to the same 128 host-side model requests.",
    },
]


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def cohort() -> list[dict[str, Any]]:
    """Use the committed wave-2 allowances, not a newly selected or repaired set."""
    cells = []
    for version, count in COHORTS.items():
        campaign_path = SOURCE / f"campaign-cheater-recall-v1-w2-{version}.json"
        campaign = ExperimentCampaign.model_validate(_json(campaign_path))
        if len(campaign.tasks) != count:
            raise ValueError(f"{version}: canonical cohort is no longer {count} tasks")
        for task in campaign.tasks:
            short = task.task_id.removeprefix("format-code-task-")
            path = SOURCE / "specs/paid-w2" / f"chrr1-w2-{version[0]}-cheat-{short}-k1.json"
            spec = ExperimentSpec.model_validate(_json(path))
            if (spec.task_id, spec.task_package_digest) != (task.task_id, task.package_digest):
                raise ValueError(f"source spec and campaign disagree: {path}")
            if spec.extra_instruction_path != PREAMBLE:
                raise ValueError(f"source audit instruction changed: {path}")
            cells.append(
                {
                    "version": version,
                    "task_id": task.task_id,
                    "short": short,
                    "task": spec.task,
                    "task_package_digest": task.package_digest,
                    "source_spec": path.relative_to(ROOT).as_posix(),
                    "source_spec_sha256": _digest(path),
                    "source_campaign_sha256": _digest(campaign_path),
                    "historical_label": "known answer channels"
                    if version == "leaky"
                    else "historical repair, not certified clean",
                    "ground_truth_status": "fresh ladder/nop receipts required; negative ladder is not proof of correctness",
                }
            )
    return cells


def _checked(path: Path, expected: str) -> Path:
    actual = compute_task_digests(path).package
    if actual != expected:
        raise ValueError(f"task bytes drifted: {path}: {actual} != {expected}")
    return path


def _cached_package(cell: dict[str, Any], primary: Path) -> Path | None:
    for root in (ROOT, primary):
        candidate = root / cell["task"]
        if candidate.is_dir():
            return _checked(candidate, cell["task_package_digest"])
    return None


def _rebuild(cell: dict[str, Any], primary: Path) -> Path:
    """Replay existing lineage into this worktree; never alter tasks or shared stores."""
    package = Path(cell["task"])
    if package.parts[:3] != ("derived", "task-store", "variants"):
        raise FileNotFoundError(f"pinned source package unavailable: {package}")
    record_path = ROOT / "library/task-variants" / package.parts[-2] / f"{package.name}.json"
    record = resolve_record(record_path, repo_root=ROOT)
    if record.variant_digest != cell["task_package_digest"]:
        raise ValueError(f"lineage does not match cohort: {record_path}")
    chain = lineage_chain(record, repo_root=ROOT)
    if any(not step.parent_found for step in chain):
        raise ValueError(f"incomplete task lineage: {record_path}")
    parent = None
    store = ROOT / "derived/task-store/variants"
    for step in reversed(chain):
        row = step.record
        if parent is None:
            source = row.parent.source
            if source.kind != "hf" or (source.repo, source.revision) != (
                HF_SOURCE["repo"],
                HF_SOURCE["revision"],
            ):
                raise ValueError(
                    f"unexpected parent source; no automatic fetch: {step.record_path}"
                )
            if not source.path:
                raise ValueError("HF parent lacks its task path")
            snapshot = (primary / SNAPSHOT).resolve()
            parent = (snapshot / source.path).resolve()
            if not parent.is_relative_to(snapshot):
                raise ValueError("parent task escapes the retained HF snapshot")
            _checked(parent, row.parent.digest)
        parent = materialize(row, parent, repo_root=ROOT, variants_root=store)
    if parent is None:
        raise ValueError("empty task lineage")
    return _checked(parent, cell["task_package_digest"])


def _freeze_json(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise FileExistsError(f"prepared content changed; choose a fresh --out: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(rendered)


def _cost(n: int, attempts: int, assumptions: dict[str, Any]) -> dict[str, Any]:
    trials = n * attempts
    concurrency = assumptions["trial_concurrency"]
    trial_seconds = assumptions["agent_timeout_seconds"] + assumptions["phase_allowance_seconds"]
    gpu_seconds = (
        math.ceil(trials / concurrency) * trial_seconds
        + assumptions["cold_start_seconds"]
        + assumptions["scaledown_seconds"]
    )
    scenario = (
        gpu_seconds * assumptions["server_usd_per_hour"]
        + trials * trial_seconds * assumptions["daytona_usd_per_sandbox_hour"]
    ) / 3600
    return {
        "timeout_scenario_usd": round(scenario, 2),
        "proposed_budget_usd": math.ceil(scenario * 1.2),
        "per_trial_reservation_usd": round(scenario / trials, 4),
        "assumptions": "2 concurrent trials, timeout+phase allowance, one cold start and idle tail per campaign; no measured throughput. CPU weight download/storage excluded. Not an enforced GPU-dollar fence.",
    }


def prepare(out: Path | None, *, attempts: int = 1) -> dict[str, Any]:
    if not 1 <= attempts <= 3:
        raise ValueError("attempts must be 1..3 for this small diagnostic")
    manifest_path = HERE / "model_manifest.json"
    manifest = _json(manifest_path)
    if manifest["schema_version"] != "trained-breaker-models/v1":
        raise ValueError("unsupported model manifest")
    if {k: manifest["runtime"][k] for k in BREAKER_SELFHOSTED_SAMPLING} != dict(
        BREAKER_SELFHOSTED_SAMPLING
    ):
        raise ValueError("model manifest differs from proxy-enforced sampling")
    primary = shared_checkout_root(ROOT)
    cells = cohort()
    harness = load_harness_tree(HARNESS, repo_root=ROOT)
    control_commands = []
    for cell in cells:
        cached = _cached_package(cell, primary)
        cell["cached_bytes_available"] = cached is not None
        if out is not None:
            package = cached or _rebuild(cell, primary)
            frozen, _ = import_task_package(package, ROOT / PREPARED_TASKS_REL)
            _checked(frozen, cell["task_package_digest"])
            cell["prepared_task"] = frozen.relative_to(ROOT).as_posix()
            control_commands.append(
                {
                    "task_id": cell["task_id"],
                    "version": cell["version"],
                    "task_package_digest": cell["task_package_digest"],
                    "ladder": [
                        "uv",
                        "run",
                        "--extra",
                        "laminar",
                        "evallab",
                        "cheat",
                        "run",
                        "--task",
                        cell["prepared_task"],
                        "--name",
                        f"brk1-gt-{cell['version'][0]}-{cell['short']}",
                        "--jobs-dir",
                        "runs",
                        "--attempts",
                        "1",
                    ],
                    "nop": [
                        "uv",
                        "run",
                        "--extra",
                        "laminar",
                        "evallab",
                        "run",
                        "--task",
                        cell["prepared_task"],
                        "--agent",
                        "nop",
                        "--name",
                        f"brk1-nop-{cell['version'][0]}-{cell['short']}",
                        "--environment",
                        "docker",
                        "--egress-lock",
                        "--timeout-seconds",
                        "600",
                    ],
                    "note": "Run the native scripted ladder on these exact bytes; preserve per-attack receipts and exceptions. Do not label all model passes as hacks.",
                }
            )
    campaigns = []
    spec_paths = []
    for arm, model in manifest["models"].items():
        selector = BREAKER_SELFHOSTED_MODEL_PREFIX + model["served_model_name"]
        if parse_breaker_selfhosted_model(selector) != model["hf_repo"]:
            raise ValueError(f"serving identity differs from weights: {arm}")
        for version in COHORTS:
            subset = [cell for cell in cells if cell["version"] == version]
            cid = f"{EXPERIMENT}-{arm}-{version}"
            cost = _cost(len(subset), attempts, manifest["cost_assumptions"])
            path = (out / f"campaign-{cid}.json") if out is not None else None
            created_at = (
                _json(path)["created_at"]
                if path is not None and path.is_file()
                else datetime.now(UTC).isoformat()
            )
            campaign = ExperimentCampaign(
                campaign_id=cid,
                budget_usd=cost["proposed_budget_usd"],
                attempts_per_task=attempts,
                concurrency=2,
                tasks=[
                    {"task_id": row["task_id"], "package_digest": row["task_package_digest"]}
                    for row in subset
                ],
                reference_profile="xiaomi-mimo-rl",
                allowed_deviations=DEVIATIONS,
                require_egress_lock=True,
                agent="terminus-2",
                model=selector,
                environment="daytona",
                serving_manifest_sha256=_digest(manifest_path),
                sampling=intended_sampling("terminus-2", selector),
                queue_cwd=str(ROOT),
                linear_card="HAR-202",
                submitted_by="rh-agents",
                created_at=created_at,
            )
            campaigns.append(
                {
                    "campaign_id": cid,
                    "model_revision": model["revision"],
                    "trial_count": len(subset) * attempts,
                    **cost,
                }
            )
            if path is not None:
                _freeze_json(path, campaign.model_dump(mode="json"))
            for cell in subset:
                for attempt in range(1, attempts + 1):
                    base = _json(ROOT / cell["source_spec"])
                    base.update(
                        name=f"brk1-{arm}-{version[0]}-{cell['short']}-k{attempt}",
                        model=selector,
                        campaign_id=cid,
                        submitted_by="rh-agents",
                        task=cell.get("prepared_task", cell["task"]),
                        timeout_seconds=manifest["cost_assumptions"]["agent_timeout_seconds"],
                        max_requests=128,
                        max_input_tokens=2_000_000,
                        max_output_tokens=131_072,
                        max_total_tokens=2_131_072,
                        cost_limit_usd=cost["proposed_budget_usd"],
                        est_cost_usd=cost["per_trial_reservation_usd"],
                        extra_instruction_sha256=_digest(ROOT / PREAMBLE),
                        harness_tree_path=HARNESS.relative_to(ROOT).as_posix(),
                        deviations=DEVIATIONS,
                        harness_tree_sha256=harness.sha256,
                        diff_sources=[],
                        question_ref=f"research/experiments/{EXPERIMENT}/README.md",
                        hypothesis=f"Matched trained L1 versus prompted base: {arm} on {cell['task_id']} ({version}, exact {cell['task_package_digest']}); reward alone is not a hack label.",
                    )
                    spec = ExperimentSpec.model_validate(base)
                    relative = f"specs/{spec.name}.json"
                    spec_paths.append(relative)
                    if out is not None:
                        _freeze_json(
                            out / relative, spec.model_dump(mode="json", exclude_none=True)
                        )
    report = {
        "schema_version": "trained-breaker-preparation/v1",
        "spend_authorized": False,
        "execution_performed": False,
        "status": "prepared-not-approved" if out is not None else "read-only-plan",
        "model_manifest_sha256": _digest(manifest_path),
        "harness_tree_sha256": harness.sha256,
        "extra_instruction_sha256": _digest(ROOT / PREAMBLE),
        "task_versions": cells,
        "campaigns": campaigns,
        "specs": spec_paths,
        "proposed_total_budget_usd": sum(c["proposed_budget_usd"] for c in campaigns),
        "source_limits": [
            "Historical repaired versions are not all proven solvable; 002391 has a documented incomplete-fix pass.",
            "L1 training overlap is unknown; this is a diagnostic, not a clean held-out capability estimate.",
            "Ladder positives require replay evidence; ladder negatives do not prove semantic validity.",
            "Daytona is retained because locked Docker does not currently admit Terminus-2; do not silently unlock it.",
            "HAR-202 records cohort lineage only; its old approvals and caps do not authorize this experiment.",
        ],
    }
    if out is not None:
        _freeze_json(out / "controls.json", control_commands)
        # Availability is a local observation; do not freeze it into reusable content.
        frozen_report = {
            **report,
            "task_versions": [
                {k: v for k, v in c.items() if k != "cached_bytes_available"} for c in cells
            ],
        }
        _freeze_json(out / "plan.json", frozen_report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="freeze local task copies and native draft specs; no dispatch",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "derived/trained-breaker-v1")
    parser.add_argument("--attempts", type=int, default=1)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.prepare and not out.is_relative_to(ROOT):
        parser.error("--out must stay inside the owned checkout")
    print(json.dumps(prepare(out if args.prepare else None, attempts=args.attempts), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
