#!/usr/bin/env python3
"""Standalone worker process for crash durability tests in certify-workflow."""

from __future__ import annotations

import argparse
import asyncio
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from certify_workflow.activities import (
    generate_mutants,
    run_cell,
    settle_spend,
    synthesize_tests,
    write_manifest,
)
from certify_workflow.workflows import CertifyCorpusWorkflow, CertifyPackageWorkflow


async def run_worker(target: str, task_queue: str, namespace: str = "default") -> None:
    client = await Client.connect(target, namespace=namespace)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[CertifyCorpusWorkflow, CertifyPackageWorkflow],
        activities=[generate_mutants, run_cell, settle_spend, synthesize_tests, write_manifest],
    )
    print(f"Certify worker started on {task_queue}", flush=True)
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
