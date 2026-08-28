#!/usr/bin/env python3
"""Fetch pinned DeepPlanning archives to a non-repository directory and verify bytes."""
from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

REVISION = "213876cce679f993a476d01042e13d111c0e3648"
BASE = f"https://huggingface.co/datasets/Qwen/DeepPlanning/resolve/{REVISION}"
ARCHIVES = {
    "database_en.zip": ("030c4914166d067632d92b2bba7b4dc6b065c33e3c98dbd33cd0c617f12a141d", 51063208),
    "database_level1.tar.gz": ("632a3b5d0db1fa0717474b9361b2d7102aeb7e1f00c32cb842f012ff7adf8000", 882139),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/deepplanning-upstream-raw"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, (expected_sha, expected_size) in ARCHIVES.items():
        destination = args.out / name
        if not destination.exists():
            with urllib.request.urlopen(f"{BASE}/{name}", timeout=120) as response:
                destination.write_bytes(response.read())
        data = destination.read_bytes()
        observed = hashlib.sha256(data).hexdigest()
        if observed != expected_sha or len(data) != expected_size:
            raise SystemExit(f"{name}: expected sha256:{expected_sha}/{expected_size}, observed sha256:{observed}/{len(data)}")
        print(f"{name}: sha256:{observed} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
