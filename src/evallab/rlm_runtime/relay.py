"""Episode-local relay: ONE persistent worker behind managed exec.

Runs inside the trial environment. Spawns the staged ``worker.py`` as a
long-lived subprocess (never restarted per request) and bridges its JSONL
stdio protocol to request/response files so the managed
``upload_file``/``exec`` primitives can drive it:

- ``reqs/rNNNNNN.json``  -- one JSON request object (no newline needed)
- ``resps/rNNNNNN.json`` -- one JSON response object written after the
  worker answers that exact request (strict in-order pairing by filename)

The relay adds no logic of its own: it forwards bytes between files and
the worker's stdin/stdout. Sub-LLM transport is handled separately by
``bridge.py``; the worker's ``RLM_TRAIN_PROXY_URL`` points at that
loopback bridge, which forwards verbatim to the env-owned proxy.

Usage: python3 relay.py <work_dir>
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    reqs = root / "reqs"
    resps = root / "resps"
    reqs.mkdir(parents=True, exist_ok=True)
    resps.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "RLM_TRAIN_PROXY_URL": os.environ["RLM_TRAIN_PROXY_URL"],
        "RLM_TRAIN_ROLLOUT_ID": os.environ["RLM_TRAIN_ROLLOUT_ID"],
        "RLM_TRAIN_DEPTH": os.environ.get("RLM_TRAIN_DEPTH", "1"),
    }
    worker = subprocess.Popen(
        ["python3", "-u", str(root / "worker.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        bufsize=1,
    )
    assert worker.stdin is not None and worker.stdout is not None
    (root / "worker.pid").write_text(str(worker.pid), encoding="utf-8")

    def _forward_termination(signum: int, _frame: object) -> None:
        # The backend kills only exact owned PIDs; when this relay is
        # killed, forward termination to the worker so no orphan remains.
        with contextlib.suppress(Exception):
            worker.terminate()

    signal.signal(signal.SIGTERM, _forward_termination)

    def respond(name: str, payload: dict) -> None:
        tmp = resps / (name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(resps / (name + ".json"))

    init_line = worker.stdout.readline()
    respond("_init", json.loads(init_line))

    done: set[str] = set()
    while worker.poll() is None:
        for req in sorted(reqs.glob("r*.json")):
            name = req.stem
            if name in done:
                continue
            payload = req.read_text(encoding="utf-8").strip()
            if not payload:
                continue  # still being written; be safe
            done.add(name)
            worker.stdin.write(payload + "\n")
            worker.stdin.flush()
            line = worker.stdout.readline()
            if not line:
                respond(
                    name,
                    {"id": name, "ok": False, "error": "worker closed stdout"},
                )
                return 1
            respond(name, json.loads(line))
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
