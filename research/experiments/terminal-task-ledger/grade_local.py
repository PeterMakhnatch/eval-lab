#!/usr/bin/env python3
"""Grade one terminal task package in local Docker and report the outcome.

Flow per task (single container, --rm):
  1. copy the package tests to /tests (setup's `rm -rf /tests` must never see
     a host bind),
  2. run the package setup.sh,
  3. optionally plant the skip-all attack (as the root agent would),
  4. run /tests/test.sh, then dump reward.txt + ctrf summary + guard logs.

Usage:
    uv run python research/experiments/terminal-task-ledger/grade_local.py \
        --package <task-dir> --image <image-id-or-ref> [--plant] [--name TAG]
"""

from __future__ import annotations

import argparse
import subprocess
import sys

ATTACK = "/tmp/tguard-attack"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--plant", action="store_true")
    ap.add_argument("--name", default="tguard-grade")
    ap.add_argument("--network", default="none")
    ap.add_argument("--commit-to", default="",
                    help="commit the container after setup to TAG and stop (no grade)")
    ap.add_argument("--skip-setup", action="store_true",
                    help="grade a container whose setup already ran (e.g. committed image)")
    args = ap.parse_args()

    net = [] if args.network == "host" else ["--network", args.network]
    cid = run(
        ["docker", "run", "-d", "--rm", *net, args.image, "sleep", "600"],
        check=True,
    ).stdout.strip()
    try:
        # setup BEFORE tests are materialized (setup itself does `rm -rf
        # /tests`); the harness mounts tests only after the agent exits.
        if not args.skip_setup:
            prep = run(
                ["docker", "exec", cid, "bash", "-c", "mkdir -p /var/lib/mimo /logs/verifier"],
                check=True,
            )
            print("PREP", prep.returncode)
            # The package dir is host-only; run setup from a container copy.
            run(["docker", "cp", f"{args.package}/environment/.", f"{cid}:/pkg-env"], check=True)
            setup = run(
                ["docker", "exec", cid, "bash", "/pkg-env/setup/setup.sh"],
                check=False,
            )
            print("SETUP", setup.returncode)
            print((setup.stdout + setup.stderr)[-2000:])
        else:
            print("SETUP skipped (committed image)")
        if args.commit_to:
            run(["docker", "commit", cid, args.commit_to], check=True)
            print("COMMITTED", args.commit_to)
            return 0
        run(["docker", "cp", f"{args.package}/tests/.", f"{cid}:/tests-tmp"], check=True)
        materialize = run(
            ["docker", "exec", cid, "bash", "-c", "cp -r /tests-tmp /tests && rm -rf /tests-tmp"],
            check=True,
        )
        print("MATERIALIZE_TESTS", materialize.returncode)
        snap = run(
            ["docker", "exec", cid, "bash", "-c",
             "cat /var/lib/mimo/terminal-guard-snapshot.json 2>/dev/null || echo NO_SNAPSHOT"],
        )
        print("SNAPSHOT", snap.stdout[:1500])
        if args.plant:
            run(["docker", "cp", f"{ATTACK}/sitecustomize.py",
                 f"{cid}:/usr/local/lib/python3.12/site-packages/sitecustomize.py"], check=True)
            run(["docker", "cp", f"{ATTACK}/zzskip.py",
                 f"{cid}:/usr/local/lib/python3.12/site-packages/zzskip.py"], check=True)
            print("PLANTED skip-all hook")
        grade = run(["docker", "exec", cid, "bash", "/tests/test.sh"])
        print("GRADE_EXIT", grade.returncode)
        print("GRADE_TAIL", (grade.stdout + grade.stderr)[-1500:])
        reward = run(["docker", "exec", cid, "bash", "-c", "cat /logs/verifier/reward.txt 2>/dev/null || echo NO_REWARD"])
        print("REWARD", reward.stdout.strip())
        ctrf = run(["docker", "exec", cid, "bash", "-c",
                    "python3 -c \"import json;d=json.load(open('/logs/verifier/ctrf.json'));r=d['results']['summary'];print(r)\" 2>&1 || echo NO_CTRF"])
        print("CTRF", ctrf.stdout.strip())
        quarantine = run(["docker", "exec", cid, "bash", "-c",
                          "ls -R /logs/verifier/quarantine 2>/dev/null || echo NO_QUARANTINE"])
        print("QUARANTINE", quarantine.stdout.strip()[:800])
        return 0
    finally:
        run(["docker", "rm", "-f", cid], check=False)


if __name__ == "__main__":
    sys.exit(main())
