#!/usr/bin/env python3
"""Live smoke test executing real `omp -p` tasks under Temporal SquadWorkflow."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_squad.activities import (
    launch_worker,
    read_report_state,
    route_quota_ok,
    settle_spend,
)
from temporal_squad.models import ItemPlan, SquadPlan
from temporal_squad.workflows import ItemWorkflow, SquadWorkflow

WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/worker.yml"
TARGET_MODEL = "google-antigravity/gemini-3.8-flash"
RECEIPTS_DIR_1 = Path(
    "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F3-TEMPORAL-SQUAD/receipts"
)
RECEIPTS_DIR_2 = Path.home() / "Developer/eval-lab-results/2026-11-02/F3-TEMPORAL-SQUAD"


async def main() -> None:
    print(f"[{time.strftime('%X')}] Starting live smoke runner with {TARGET_MODEL}...")

    run_dir = Path.home() / "Developer/eval-lab-results/2026-11-02/F3-TEMPORAL-SQUAD/live-smoke-run"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    RECEIPTS_DIR_1.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR_2.mkdir(parents=True, exist_ok=True)

    # 1. Setup Item 1
    raw_1 = run_dir / "raw" / "F3-LIVE-1"
    raw_1.mkdir(parents=True, exist_ok=True)
    sess_1 = run_dir / "sessions" / "F3-LIVE-1"
    sess_1.mkdir(parents=True, exist_ok=True)
    prompt_1 = raw_1 / "prompt.md"
    prompt_1.write_text(
        "You are a headless test agent for Temporal squad runner.\n"
        "Instructions:\n"
        "1. Write a 3-line haiku about durable execution to `haiku.txt` in your current directory.\n"
        "2. Write a report file `report.md` in your current directory with:\n"
        "# Report for F3-LIVE-1\nHaiku written successfully.\nDS-DONE: F3-LIVE-1\n\n"
        "Make sure the LAST line of `report.md` is exactly:\n"
        "DS-DONE: F3-LIVE-1\n"
    )

    cmd_1 = [
        "omp",
        "-p",
        "--model",
        TARGET_MODEL,
        "--config",
        WORKER_YML,
        "--no-skills",
        "--session-dir",
        str(sess_1),
        "--thinking",
        "low",
        "--tools",
        "read,bash,edit,write,grep,glob,todo",
        f"@{prompt_1}",
    ]

    item_1 = ItemPlan(
        id="F3-LIVE-1",
        title="Live OMP task 1: haiku generation",
        cmd_override=cmd_1,
        raw_dir=str(raw_1),
        session_dir=str(sess_1),
        max_minutes=5.0,
        max_usd=2.0,
    )

    # 2. Setup Item 2 (needs Item 1)
    raw_2 = run_dir / "raw" / "F3-LIVE-2"
    raw_2.mkdir(parents=True, exist_ok=True)
    sess_2 = run_dir / "sessions" / "F3-LIVE-2"
    sess_2.mkdir(parents=True, exist_ok=True)
    prompt_2 = raw_2 / "prompt.md"
    prompt_2.write_text(
        "You are a headless test agent for Temporal squad runner.\n"
        "Instructions:\n"
        "1. Check if `haiku.txt` exists in `../F3-LIVE-1/haiku.txt`. If so, read it, or write a 3-line poem on durability to `poem.txt`.\n"
        "2. Write a report file `report.md` in your current directory with:\n"
        "# Report for F3-LIVE-2\nVerification completed.\nDS-DONE: F3-LIVE-2\n\n"
        "Make sure the LAST line of `report.md` is exactly:\n"
        "DS-DONE: F3-LIVE-2\n"
    )

    cmd_2 = [
        "omp",
        "-p",
        "--model",
        TARGET_MODEL,
        "--config",
        WORKER_YML,
        "--no-skills",
        "--session-dir",
        str(sess_2),
        "--thinking",
        "low",
        "--tools",
        "read,bash,edit,write,grep,glob,todo",
        f"@{prompt_2}",
    ]

    item_2 = ItemPlan(
        id="F3-LIVE-2",
        title="Live OMP task 2: haiku verification",
        needs=["F3-LIVE-1"],
        cmd_override=cmd_2,
        raw_dir=str(raw_2),
        session_dir=str(sess_2),
        max_minutes=5.0,
        max_usd=2.0,
    )

    plan = SquadPlan(
        model=TARGET_MODEL,
        budget_usd=10.0,
        max_parallel=2,
        subscription=True,
        run_dir=str(run_dir),
        items=[item_1, item_2],
    )

    task_queue = "live-smoke-queue"
    print(f"[{time.strftime('%X')}] Starting local dev server with ephemeral port...")

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client

        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[SquadWorkflow, ItemWorkflow],
            activities=[launch_worker, read_report_state, route_quota_ok, settle_spend],
        )

        async with worker:
            wf_id = f"live-smoke-{int(time.time())}"
            print(f"[{time.strftime('%X')}] Starting workflow {wf_id}...")
            handle = await client.start_workflow(
                SquadWorkflow.run,
                plan,
                id=wf_id,
                task_queue=task_queue,
            )

            # Monitor progress
            t0 = time.time()
            while True:
                desc = await handle.describe()
                if desc.status.name != "RUNNING":
                    break
                try:
                    board_text = await handle.query(SquadWorkflow.board)
                    print(f"--- Live Board ({time.strftime('%X')}) ---")
                    print(board_text.strip())
                except Exception:
                    pass
                await asyncio.sleep(5.0)

            print(f"[{time.strftime('%X')}] Workflow finished! Awaiting final result...")
            result = await handle.result()
            print("Final Result Status:")
            print(json.dumps(result, indent=2))

            # Fetch workflow execution history and export as JSON receipt
            print(f"[{time.strftime('%X')}] Fetching workflow execution history...")
            history = await handle.fetch_history()
            history_json = history.to_json()

            history_file_1 = RECEIPTS_DIR_1 / "live_workflow_history.json"
            history_file_2 = RECEIPTS_DIR_2 / "live_workflow_history.json"
            history_file_1.write_text(history_json)
            history_file_2.write_text(history_json)
            print(f"Saved history receipt to {history_file_1} and {history_file_2}")

            # Verify session attestations
            attestations = {}
            for iid in ["F3-LIVE-1", "F3-LIVE-2"]:
                sess_dir = run_dir / "sessions" / iid
                served_models = {}
                fallback_events = 0
                for jsonl_file in sess_dir.rglob("*.jsonl"):
                    for line in jsonl_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if not line.strip():
                            continue
                        if '"resolvedModelIsFallback": true' in line or '"resolvedModelIsFallback":true' in line:
                            fallback_events += 1
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        msg = rec.get("message")
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            k = f"{msg.get('provider')}/{msg.get('model')}"
                            served_models[k] = served_models.get(k, 0) + 1

                attestations[iid] = {
                    "requested": TARGET_MODEL,
                    "served": served_models,
                    "off_route": {k: v for k, v in served_models.items() if k != TARGET_MODEL},
                    "fallback_events": fallback_events,
                }

            print("Session Attestation:")
            print(json.dumps(attestations, indent=2))

            summary = {
                "workflow_id": wf_id,
                "elapsed_sec": time.time() - t0,
                "states": result.get("states", {}),
                "attestations": attestations,
            }
            summary_json = json.dumps(summary, indent=2)
            (RECEIPTS_DIR_1 / "live_summary.json").write_text(summary_json)
            (RECEIPTS_DIR_2 / "live_summary.json").write_text(summary_json)

            # Assertions on live run
            states = result.get("states", {})
            assert states.get("F3-LIVE-1", {}).get("status") == "done", "F3-LIVE-1 did not complete done"
            assert states.get("F3-LIVE-2", {}).get("status") == "done", "F3-LIVE-2 did not complete done"

            for iid, att in attestations.items():
                assert att["fallback_events"] == 0, f"Fallback events occurred for {iid}: {att}"
                assert not att["off_route"], f"Off-route models served for {iid}: {att}"
                assert TARGET_MODEL in att["served"], f"{TARGET_MODEL} was not served for {iid}"

            print("LIVE SMOKE PASSED ALL ATTESTATIONS AND CHECKS!")


if __name__ == "__main__":
    asyncio.run(main())
