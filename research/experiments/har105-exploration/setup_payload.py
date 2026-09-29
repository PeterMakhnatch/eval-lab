#!/usr/bin/env python3
"""Re-embed a MiMo task's ``environment/setup/`` folder into its healthcheck.

The ``mimo_harbor`` adapter does not run ``environment/setup/`` from disk.
``[environment.healthcheck].command`` carries the folder as an inline
base64 tar.gz; the healthcheck unpacks it into ``/var/lib/mimo`` and runs
``setup.sh``. The on-disk folder is only a readable copy. A fix that edits
``setup.sh`` must therefore also re-encode the payload, or it never runs.

The encoding matches the adapter byte for byte: every file is a tar member
with mode 0700, mtime 0 and empty owner names, and the gzip header has
mtime 0. ``--check`` proves this by re-encoding a task and comparing with
the payload already in its ``task.toml``.

    setup_payload.py --check TASK_DIR [TASK_DIR ...]
    setup_payload.py TASK_DIR OUT_TOML   # task.toml with TASK_DIR's setup/ embedded
"""

from __future__ import annotations

import base64
import gzip
import io
import re
import sys
import tarfile
import tomllib
from pathlib import Path

PAYLOAD = re.compile(r"echo ([A-Za-z0-9+/=]{64,}) \| base64 -d \| tar -xzf - -C /var/lib/mimo")


def encode(setup: Path) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(p for p in setup.rglob("*") if p.is_file()):
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(setup).as_posix())
            info.size, info.mode, info.mtime = len(data), 0o700, 0
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(gzip.compress(raw.getvalue(), mtime=0)).decode()


def embedded(task: Path) -> str:
    command = tomllib.loads((task / "task.toml").read_text())["environment"]["healthcheck"][
        "command"
    ]
    match = PAYLOAD.search(command)
    if match is None:
        raise SystemExit(f"{task}: healthcheck carries no setup payload")
    return match.group(1)


def main(argv: list[str]) -> None:
    if argv[:1] == ["--check"]:
        for task in map(Path, argv[1:]):
            same = encode(task / "environment/setup") == embedded(task)
            print(task.name, "payload reproduced" if same else "PAYLOAD DIFFERS")
            if not same:
                raise SystemExit(1)
        return
    task, out = Path(argv[0]), Path(argv[1])
    text = (task / "task.toml").read_text()
    old = embedded(task)
    if text.count(old) != 1:
        raise SystemExit(f"{task}: payload appears {text.count(old)} times in task.toml")
    out.write_text(text.replace(old, encode(task / "environment/setup")))


if __name__ == "__main__":
    main(sys.argv[1:])
