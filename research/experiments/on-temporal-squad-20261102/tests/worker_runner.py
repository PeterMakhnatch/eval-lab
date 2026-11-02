#!/usr/bin/env python3
"""Standalone worker process for crash durability tests."""
from __future__ import annotations

import argparse
import asyncio
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from temporal_squad.activities import (
    launch_worker,
    read_report_state,
    route_quota_ok,
    settle_spend,
)
from temporal_squad.workflows import ItemWorkflow, SquadWorkflow


async def run_worker(target: str, task_queue: str, namespace: str = "default") -> None:
    client = await Client.connect(target, namespace=namespace)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SquadWorkflow, ItemWorkflow],
        activities=[launch_worker, read_report_state, route_quota_ok, settle_spend],
    )
    print(f"Worker started on {task_queue}", flush=True)
    await worker.run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--task-queue", required=True)
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args()

    try:
        asyncio.run(run_worker(args.target, args.task_queue, args.namespace))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
