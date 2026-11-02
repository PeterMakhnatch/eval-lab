"""Standalone worker process runner for crash and recovery tests."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from mergetrain.activities import (
    check_ci_activity,
    merge_pr_activity,
    rebase_pr_activity,
    view_pr_activity,
)
from mergetrain.workflows import MergeTrainWorkflow


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--task-queue", required=True)
    parser.add_argument("--repo-dir", default="")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--merge-ledger", default="")
    args = parser.parse_args()

    if args.repo_dir:
        os.environ["FAKE_GH_REPO_DIR"] = args.repo_dir
    if args.state_file:
        os.environ["FAKE_GH_STATE_FILE"] = args.state_file
    if args.merge_ledger:
        os.environ["FAKE_GH_MERGE_LEDGER"] = args.merge_ledger

    client = await Client.connect(args.target)

    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=[MergeTrainWorkflow],
        activities=[
            view_pr_activity,
            rebase_pr_activity,
            check_ci_activity,
            merge_pr_activity,
        ],
    )

    stop_event = asyncio.Event()

    def _sig_handler(*_):
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(Exception):
            signal.signal(sig, _sig_handler)

    async with worker:
        while not stop_event.is_set():
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
