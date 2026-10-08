#!/usr/bin/env python3
"""Locked validation for the 10-task music-prefetch sample.

For each task: grade the parent with the network open (baseline), then set
up the variant with the network open and grade it with the network
disconnected. Appends one JSON object per step to results.jsonl.

Usage: validate_sample.py TASK [TASK ...]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

EXP = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
IMAGE = "docker.io/library/python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
OUT = EXP / "results.jsonl"


def sh(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)


def healthcheck_of(task_dir: Path) -> str:
    return tomllib.loads((task_dir / "task.toml").read_text())["environment"]["healthcheck"][
        "command"
    ]


def variant_dir_of(task_id: str) -> Path:
    task_name = f"mimo-v2.6-rl/{task_id}"
    slug = task_name.replace("/", "__")
    recs = sorted((REPO / "library/task-variants" / slug).glob("*.json"))
    assert len(recs) == 1, f"{slug}: {len(recs)} records"
    digest = json.loads(recs[0].read_text())["variant_digest"].split(":")[1][:12]
    from evallab.storage.paths import shared_checkout_root

    store = shared_checkout_root(REPO) / "derived/task-store/variants" / slug / digest
    assert store.is_dir(), f"missing variant package {store}"
    return store


def log(**row) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def grade_flow(cname: str, pkg: Path, *, answer: bool, tag: str, task_id: str) -> None:
    r = sh("docker", "exec", cname, "mkdir", "-p", "/tests", "/app")
    assert r.returncode == 0, r.stderr
    r = sh("docker", "cp", str(pkg / "tests") + "/.", f"{cname}:/tests/")
    assert r.returncode == 0, r.stderr[-2000:]
    if answer:
        r = sh("docker", "cp", str(EXP / "fixed-answer.md"), f"{cname}:/app/answer.md")
        assert r.returncode == 0, r.stderr
    else:
        sh("docker", "exec", cname, "rm", "-f", "/app/answer.md")
    r = sh("docker", "exec", cname, "bash", "/tests/test.sh", timeout=600)
    res = sh("docker", "exec", cname, "cat", "/logs/verifier/result.json")
    rew = sh("docker", "exec", cname, "cat", "/logs/verifier/reward.txt")
    log(
        task=task_id,
        tag=tag,
        answer=answer,
        exit=r.returncode,
        stdout_tail=r.stdout[-500:],
        result=res.stdout.strip(),
        reward=rew.stdout.strip(),
    )


def parent_baseline(task_id: str, parent: Path) -> None:
    cname = f"music-val-parent-{task_id}"
    sh("docker", "rm", "-f", cname)
    r = sh("docker", "run", "-d", "--name", cname, IMAGE, "sleep", "infinity")
    assert r.returncode == 0, r.stderr
    try:
        r = sh("docker", "exec", cname, "bash", "-c", healthcheck_of(parent), timeout=600)
        log(task=task_id, tag="parent-setup-net", answer=False, exit=r.returncode,
            stdout_tail=r.stdout[-300:] + r.stderr[-300:], result="", reward="")
        assert r.returncode == 0, r.stderr[-2000:]
        grade_flow(cname, parent, answer=False, tag="parent-nop-net", task_id=task_id)
        grade_flow(cname, parent, answer=True, tag="parent-fixed-net", task_id=task_id)
    finally:
        sh("docker", "rm", "-f", cname)


def variant_locked(task_id: str, variant: Path) -> None:
    cname = f"music-val-variant-{task_id}"
    sh("docker", "rm", "-f", cname)
    r = sh("docker", "run", "-d", "--name", cname, IMAGE, "sleep", "infinity")
    assert r.returncode == 0, r.stderr
    try:
        r = sh("docker", "exec", cname, "bash", "-c", healthcheck_of(variant), timeout=900)
        log(task=task_id, tag="variant-setup-net", answer=False, exit=r.returncode,
            stdout_tail=r.stdout[-300:] + r.stderr[-300:], result="", reward="")
        assert r.returncode == 0, r.stderr[-2000:]
        info = sh(
            "docker", "exec", cname, "bash", "-c",
            "command -v abc2midi; dpkg -s abcmidi | grep -E '^(Version|Architecture)'; "
            "dpkg --print-architecture; cat /etc/os-release | head -n 3",
        )
        log(task=task_id, tag="variant-abc2midi-info", answer=False, exit=info.returncode,
            stdout_tail=info.stdout.strip(), result="", reward="")
        r = sh("docker", "network", "disconnect", "bridge", cname)
        assert r.returncode == 0, r.stderr
        net = sh("docker", "exec", cname, "ls", "/sys/class/net/")
        locked = "eth0" not in net.stdout.split()
        log(task=task_id, tag="variant-net-state", answer=False, exit=0 if locked else 1,
            stdout_tail=";".join(sorted(net.stdout.split())), result="", reward="")
        grade_flow(cname, variant, answer=False, tag="variant-nop-locked", task_id=task_id)
        grade_flow(cname, variant, answer=True, tag="variant-fixed-locked", task_id=task_id)
    finally:
        sh("docker", "rm", "-f", cname)


def main(tasks: list[str]) -> None:
    sys.path.insert(0, str(REPO / "src"))
    snap = (
        __import__("evallab.storage.paths", fromlist=["shared_checkout_root"])
        .shared_checkout_root(REPO)
        / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-music@e1a66d4553ee/tasks"
    )
    for task_id in tasks:
        parent_baseline(task_id, snap / task_id)
        variant_locked(task_id, variant_dir_of(task_id))


if __name__ == "__main__":
    main(sys.argv[1:])
