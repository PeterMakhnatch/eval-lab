"""Preview a prepared arm; execute only with existing exact native approvals.

Uses Eval Lab's queue and tick, never a separate Harbor runner. Execution stops
this experiment's selected Modal app on exit. It never deploys or approves one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from evallab.campaign_approval import (
    ExperimentCampaign,
    campaign_content_digest,
    load_approved_campaign,
    validate_campaign_content,
)
from evallab.execution_contracts import (
    BREAKER_SELFHOSTED_KEY_ENV,
    BREAKER_SELFHOSTED_UPSTREAM_ENV,
    breaker_selfhosted_endpoint,
)
from evallab.schemas import ExperimentSpec

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=ROOT / "derived/trained-breaker-v1")
    parser.add_argument("--arm", choices=("baseline", "l1"), required=True)
    parser.add_argument("--cohort", choices=("leaky", "hardened", "all"), default="all")
    parser.add_argument(
        "--execute", action="store_true", help="requires prior native campaign approval; may spend"
    )
    args = parser.parse_args()
    prepared = args.prepared.resolve()
    if not prepared.is_relative_to(ROOT):
        parser.error("prepared files must belong to this checkout")
    plan = json.loads((prepared / "plan.json").read_text())
    manifest_path = HERE / "model_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if plan["model_manifest_sha256"] != "sha256:" + hashlib.sha256(manifest_bytes).hexdigest():
        parser.error("serving pins changed; prepare and approve fresh campaign files")
    cohorts = ("leaky", "hardened") if args.cohort == "all" else (args.cohort,)
    campaign_ids = {f"trained-breaker-v1-{args.arm}-{cohort}" for cohort in cohorts}
    drafts = [
        ExperimentCampaign.model_validate_json((prepared / f"campaign-{cid}.json").read_text())
        for cid in sorted(campaign_ids)
    ]
    specs = []
    for relative in plan["specs"]:
        path = (prepared / relative).resolve()
        if not path.is_relative_to(prepared):
            parser.error("spec path escapes prepared bundle")
        spec = ExperimentSpec.model_validate_json(path.read_text())
        if spec.campaign_id in campaign_ids:
            specs.append(spec)
    if not specs:
        parser.error("prepared arm contains no trials")
    stop = [
        "uvx",
        "--from",
        "modal==1.5.4",
        "modal",
        "app",
        "stop",
        manifest["serving"]["app_per_arm"][args.arm],
    ]
    print(
        json.dumps(
            {
                "mode": "execution-requested" if args.execute else "preview-no-spend",
                "arm": args.arm,
                "trials": len(specs),
                "campaigns": sorted(campaign_ids),
                "proposed_budget_usd": sum(c.budget_usd for c in drafts),
                "required_upstream_env": BREAKER_SELFHOSTED_UPSTREAM_ENV,
                "stop_on_exit": stop,
            },
            indent=2,
        ),
        flush=True,
    )
    if not args.execute:
        return 0
    # Every permission/config check precedes queue mutation or remote activity.
    for draft in drafts:
        approved, _ = load_approved_campaign(ROOT, draft.campaign_id)
        if campaign_content_digest(approved) != campaign_content_digest(draft):
            parser.error(f"approval differs from prepared campaign {draft.campaign_id}")
        errors = validate_campaign_content(ROOT, approved)
        if errors:
            parser.error("; ".join(errors))
    upstream = os.environ.get(BREAKER_SELFHOSTED_UPSTREAM_ENV, "")
    if not breaker_selfhosted_endpoint(upstream).startswith("https://"):
        parser.error("real comparison requires the approved remote Modal origin, not a smoke peer")
    if not os.environ.get(BREAKER_SELFHOSTED_KEY_ENV):
        parser.error(
            f"supply {BREAKER_SELFHOSTED_KEY_ENV} with keys run; no key value belongs in files"
        )
    receipt = prepared / f"launch-{args.arm}-{args.cohort}.json"
    payload = {
        "arm": args.arm,
        "campaigns": sorted(campaign_ids),
        "spec_ids": [],
        "upstream": upstream,
        "status": "submitting",
    }
    # Refuse accidental resubmission; native queue events remain authoritative.
    with receipt.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
    result = 1
    try:
        from evallab.queue import Executor

        os.environ["EVALLAB_DERIVED_ROOT"] = str(ROOT / "derived/parquet")
        executor = Executor.from_repo(ROOT, parallel=2)
        for spec in specs:
            path, decision = executor.submit(spec)
            queued = executor.queue.load(path)
            payload["spec_ids"].append(queued.spec_id)
            receipt.write_text(json.dumps(payload, indent=2) + "\n")
            if not decision.admitted:
                raise RuntimeError(f"native policy refused {spec.name}: {decision.reason_code}")
        command = [
            "uv",
            "run",
            "--extra",
            "laminar",
            "evallab",
            "tick",
            "--parallel",
            "2",
            "--max-active-trials",
            "2",
            "--selfhosted-warmup-seconds",
            "1200",
        ]
        for spec_id in payload["spec_ids"]:
            command.extend(["--spec-id", str(spec_id)])
        payload["tick_argv"] = command
        receipt.write_text(json.dumps(payload, indent=2) + "\n")
        env = {**os.environ, "EVALLAB_WATCH_STOP": "dry-run"}
        result = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
        payload["status"] = "native-tick-completed" if result == 0 else "native-tick-incomplete"
    finally:
        # These dedicated apps belong to this experiment, never the MiMo service.
        stopped = subprocess.run(stop, cwd=ROOT, check=False)
        payload["stop_returncode"] = stopped.returncode
        receipt.write_text(json.dumps(payload, indent=2) + "\n")
        if stopped.returncode != 0:
            print("Modal shutdown failed; use the recorded stop command immediately.", flush=True)
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
