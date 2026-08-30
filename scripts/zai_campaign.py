#!/usr/bin/env python3
"""Durable campaign compiler and runner for the cost-bounded Action Memory plan.

Deterministic CLI for compiling, checking, launching, and inspecting the
38-trial conditional Z.ai Action Memory campaign (36-trial measured-dose phase A
plus the two-trial 128k cost canary in phase B).

SECURITY:
    The CLI stages a provider-only (Z.ai) auth document from OpenCode auth
    without printing, logging, or serialising credential material. All printed
    auth status is non-secret (provider keys, booleans, and counts only).

Usage:
    # Compile the certified 38-trial manifest
    python scripts/zai_campaign.py compile --task-root library/tasks -o manifest.json

    # Preflight budgets, allowlist, auth presence and task inputs
    python scripts/zai_campaign.py check --task-root library/tasks

    # Launch / resume the state machine (dry-run: compile + record)
    python scripts/zai_campaign.py launch --task-root library/tasks --dry-run

    # Inspect resumable status
    python scripts/zai_campaign.py status

    # Matched-contrast coverage report
    python scripts/zai_campaign.py report --task-root library/tasks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evallab.zai_campaign import (  # noqa: E402
    ALLOWED_MODELS,
    CAMPAIGN_ID,
    DEFAULT_OPENCODE_AUTH,
    STATE_ROOT_DEFAULT,
    TOTAL_TRIALS,
    ZaiCampaignAuthError,
    ZaiCampaignBudgetError,
    ZaiCampaignError,
    ZaiCampaignModelError,
    ZaiCampaignPreconditionError,
    ZaiCampaignRunner,
    ZaiCampaignTaskError,
    build_default_definition,
    compile_campaign,
    describe_auth_shape,
    load_definition,
    matched_contrast_report,
    read_opencode_auth,
    validate_model,
)


def _resolve_definition(args: argparse.Namespace) -> Any:
    if getattr(args, "definition", None) is not None:
        return load_definition(Path(args.definition))
    lane_model = getattr(args, "lane_model", "zai-coding-plan/glm-5.3-flash")
    max_concurrency = getattr(args, "max_concurrency", 1)
    return build_default_definition(
        lane_model=lane_model,
        max_concurrency=max_concurrency,
    )


def cmd_compile(args: argparse.Namespace) -> int:
    try:
        definition = _resolve_definition(args)
        manifest = compile_campaign(definition, task_root=Path(args.task_root))
    except (ZaiCampaignError, ValueError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1

    payload = manifest.model_dump(mode="json")
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote manifest ({manifest.total_trials} trials): {out}")
    elif args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"campaign_id:     {manifest.campaign_id}")
        print(f"manifest_digest: {manifest.manifest_digest}")
        print(f"phase_a_trials:  {len(manifest.phase_a)} (measured 4k/16k/64k)")
        print(f"phase_b_trials:  {len(manifest.phase_b)} (128k cost canary)")
        print(f"total_trials:    {manifest.total_trials}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        definition = _resolve_definition(args)
        runner = ZaiCampaignRunner(
            definition=definition,
            task_root=Path(args.task_root),
            auth_path=Path(args.auth),
        )
        runner.preflight(require_isolation=args.require_isolation)
    except (
        ZaiCampaignBudgetError,
        ZaiCampaignModelError,
        ZaiCampaignAuthError,
        ZaiCampaignTaskError,
        ZaiCampaignPreconditionError,
        ZaiCampaignError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1

    doc = read_opencode_auth(Path(args.auth))
    shape = describe_auth_shape(doc)
    if args.json:
        print(json.dumps({"ok": True, "auth_shape": shape.to_redacted()}, indent=2))
    else:
        print("preflight: passed")
        print(f"  model:             {definition.lane_model}")
        print(f"  trials:            {TOTAL_TRIALS}")
        print(f"  token_budget:      {definition.limits.prompt_token_budget}")
        print(f"  auth_zai_present:  {shape.zai_present}")
        print(f"  provider_keys:     {list(shape.retained_provider_keys)}")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    try:
        definition = _resolve_definition(args)
        runner = ZaiCampaignRunner(
            definition=definition,
            task_root=Path(args.task_root),
            auth_path=Path(args.auth),
            state_root=Path(args.state_root),
        )
        status = runner.run(resume=args.resume, dry_run=args.dry_run)
    except (ZaiCampaignError, ValueError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1

    payload = status.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"campaign_id:     {status.campaign_id}")
        print(f"state:           {status.state}")
        print(f"manifest_digest: {status.manifest_digest}")
        print(f"attempts:        {len(status.attempts)}")
        print(f"tokens_used:     {status.prompt_tokens_used}")
        if status.phase_b_skipped:
            print(f"phase_b:         skipped ({status.phase_b_reason})")
        else:
            print("phase_b:         executed")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from evallab.zai_campaign import ZaiCampaignState

    state = ZaiCampaignState(
        Path(args.state_root),
        args.campaign_id,
        manifest_digest="sha256:" + "0" * 64,
    )
    status = state.load()
    if status is None:
        sys.stderr.write(f"no campaign state for {args.campaign_id} under {args.state_root}\n")
        return 1

    payload = status.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"campaign_id:     {status.campaign_id}")
        print(f"state:           {status.state}")
        print(f"manifest_digest: {status.manifest_digest}")
        print(f"attempts:        {len(status.attempts)}")
        print(f"tokens_used:     {status.prompt_tokens_used}")
        if status.phase_b_skipped:
            print(f"phase_b:         skipped ({status.phase_b_reason})")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from evallab.zai_campaign import ZaiCampaignState

    try:
        definition = _resolve_definition(args)
        manifest = compile_campaign(definition, task_root=Path(args.task_root))
    except (ZaiCampaignError, ValueError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1

    state = ZaiCampaignState(
        Path(args.state_root),
        definition.campaign_id,
        manifest.manifest_digest,
    )
    status = state.load()
    attempts = status.attempts if status else ()
    rows = matched_contrast_report(manifest, attempts=attempts)

    if args.json:
        records = [
            {
                "task_block_id": row.task_block_id,
                "dose_bytes": row.dose_bytes,
                "seed": row.seed,
                "planned_trials": row.planned_trials,
                "scored": row.scored,
                "non_scored": row.non_scored,
                "unknown": row.unknown,
                "omitted": row.omitted,
                "duplicate": row.duplicate,
                "order_fidelity": row.order_fidelity,
                "arms": list(row.arms_present),
            }
            for row in rows
        ]
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print(f"Matched contrasts ({len(rows)} blocks):")
        for row in rows:
            print(
                f"  {row.task_block_id:<32} dose={row.dose_bytes:<7} seed={row.seed:<5} "
                f"planned={row.planned_trials} scored={row.scored} non_scored={row.non_scored}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile and run the cost-bounded Action Memory campaign (Z.ai lane)."
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=STATE_ROOT_DEFAULT,
        help=f"campaign durable state directory (default: {STATE_ROOT_DEFAULT})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # compile
    p_compile = sub.add_parser("compile", help="compile the 38-trial manifest")
    p_compile.add_argument("--task-root", type=Path, default=Path("library/tasks"))
    p_compile.add_argument("-o", "--output", type=Path, help="write manifest JSON to file")
    p_compile.add_argument("--definition", type=Path, help="campaign definition JSON")
    p_compile.add_argument(
        "--lane-model",
        default="zai-coding-plan/glm-5.3-flash",
        choices=sorted(ALLOWED_MODELS),
    )
    p_compile.add_argument("--json", action="store_true")
    p_compile.set_defaults(func=cmd_compile)

    # check
    p_check = sub.add_parser("check", help="preflight budgets, models, auth and tasks")
    p_check.add_argument("--task-root", type=Path, default=Path("library/tasks"))
    p_check.add_argument("--auth", type=Path, default=DEFAULT_OPENCODE_AUTH)
    p_check.add_argument("--definition", type=Path, help="campaign definition JSON")
    p_check.add_argument(
        "--lane-model",
        default="zai-coding-plan/glm-5.3-flash",
        choices=sorted(ALLOWED_MODELS),
    )
    p_check.add_argument(
        "--require-isolation",
        action="store_true",
        help="refuse unless host isolation and credential proxy are declared",
    )
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    # launch
    p_launch = sub.add_parser("launch", help="run or resume the campaign state machine")
    p_launch.add_argument("--task-root", type=Path, default=Path("library/tasks"))
    p_launch.add_argument("--auth", type=Path, default=DEFAULT_OPENCODE_AUTH)
    p_launch.add_argument("--definition", type=Path, help="campaign definition JSON")
    p_launch.add_argument(
        "--lane-model",
        default="zai-coding-plan/glm-5.3-flash",
        choices=sorted(ALLOWED_MODELS),
    )
    p_launch.add_argument("--resume", action="store_true", help="resume an existing run")
    p_launch.add_argument(
        "--dry-run",
        action="store_true",
        help="compile and record planned attempts without a live provider call",
    )
    p_launch.add_argument("--json", action="store_true")
    p_launch.set_defaults(func=cmd_launch)

    # status
    p_status = sub.add_parser("status", help="inspect campaign status")
    p_status.add_argument("--campaign-id", default=CAMPAIGN_ID)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # report
    p_report = sub.add_parser("report", help="matched-contrast coverage and fidelity")
    p_report.add_argument("--task-root", type=Path, default=Path("library/tasks"))
    p_report.add_argument("--definition", type=Path, help="campaign definition JSON")
    p_report.add_argument(
        "--lane-model",
        default="zai-coding-plan/glm-5.3-flash",
        choices=sorted(ALLOWED_MODELS),
    )
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
