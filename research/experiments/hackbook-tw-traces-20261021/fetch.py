"""Fetch the Terminal Wrench trajectories used by ``run.py`` (pB-SELF-7), resumable.

Pinned to the dataset revision and index hashes recorded in
``research/answer-keys/breaker-bench-v1/upstream/terminal_wrench_tasks.json``. Downloads, per task,
the ``stripped_trajectories`` of every ``rewarded_serious_exploit`` hack (red-team prompt removed from
the first message, agent text blanked) and every legitimate passing ``baseline_trajectories`` run,
about 10 KB each. Writes ``manifest.json`` (label rows plus the DEV/TEST task split) next to the cache.

    python fetch.py --cache DIR            # about 5,500 files
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
KEY = REPO / "research/answer-keys/breaker-bench-v1"
BASE = "https://huggingface.co/datasets/few-sh/terminal-wrench/resolve/"


def fetch_bytes(url: str, tries: int = 8) -> bytes:
    for attempt in range(tries):
        delay = 2**attempt * 0.5
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == tries - 1:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = max(delay, float(retry_after)) if retry_after.isdigit() else delay
        except OSError:  # URLError, resets, timeouts
            if attempt == tries - 1:
                raise
        time.sleep(min(delay, 60.0))
    raise AssertionError("unreachable")


def pinned() -> dict[str, Any]:
    return json.loads((KEY / "upstream/terminal_wrench_tasks.json").read_text())


def load_index(cache: Path, pin: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    out = []
    for name, field in (
        ("tasks", "index_tasks_sha256"),
        ("trajectories", "index_trajectories_sha256"),
    ):
        path = cache / f"{name}.json"
        if not path.exists():
            path.write_bytes(fetch_bytes(f"{BASE}{pin['hf_revision']}/index/{name}.json"))
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != pin[field]:
            raise SystemExit(f"{path} hash {digest} != pinned {pin[field]}")
        out.append(json.loads(path.read_text()))
    return out[0], out[1]


def split_tasks(all_tasks: set[str]) -> tuple[list[str], list[str]]:
    """DEV = the 139 auditor-sft train tasks + breaker-bench's dev tasks; TEST = its test tasks."""
    items = [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines() if line]
    held = {
        i["subject"]["task_id"].split("/", 1)[1]: i["split"]
        for i in items
        if i["source_set"] == "terminal_wrench_holdout"
    }
    train = all_tasks - set(held)
    dev = sorted(train | {t for t, s in held.items() if s == "dev"})
    test = sorted(t for t, s in held.items() if s == "test")
    return dev, test


def build_manifest(tasks_idx: list[dict], traj_idx: list[dict]) -> dict[str, Any]:
    dev, test = split_tasks({t["task_id"] for t in tasks_idx})
    split = {**{t: "dev" for t in dev}, **{t: "test" for t in test}}
    rows: list[dict[str, Any]] = []
    for j in traj_idx:
        if j.get("classification") != "rewarded_serious_exploit":
            continue
        rows.append(
            {
                "id": f"hack:{j['task_id']}:{j['model']}:{j['trajectory_label']}",
                "kind": "hack",
                "task": j["task_id"],
                "split": split[j["task_id"]],
                "model": j["model"],
                "path": f"tasks/{j['task_id']}/{j['model']}/stripped_trajectories/"
                f"{j['trajectory_label']}/trial/agent/trajectory.json",
                "categories": j.get("observed_categories", []),
                "key_hack_steps": j.get("key_hack_steps", []),
                "summary": j.get("brief_exploit_summary", ""),
                "source_dataset": j["source_dataset"],
            }
        )
    for t in tasks_idx:
        for b in t.get("baselines", []):
            if b.get("reward") != 1.0:
                continue
            rows.append(
                {
                    "id": f"base:{t['task_id']}:{t['model']}:{b['label']}",
                    "kind": "baseline",
                    "task": t["task_id"],
                    "split": split[t["task_id"]],
                    "model": t["model"],
                    "path": f"tasks/{t['task_id']}/{t['model']}/baseline_trajectories/"
                    f"{b['label']}/trial/agent/trajectory.json",
                    "categories": [],
                    "key_hack_steps": [],
                    "summary": "",
                    "source_dataset": t["source_dataset"],
                }
            )
    return {
        "dataset": pin_note(),
        "dev_tasks": dev,
        "test_tasks": test,
        "rows": sorted(rows, key=lambda r: r["id"]),
    }


def pin_note() -> str:
    pin = pinned()
    return f"{pin['hf_dataset']}@{pin['hf_revision']}"


def download(row: dict[str, Any], cache: Path, revision: str) -> str | None:
    dest = cache / "files" / row["path"]
    if dest.exists() and dest.stat().st_size:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(fetch_bytes(f"{BASE}{revision}/{row['path']}"))
    except urllib.error.HTTPError as error:
        return f"{row['id']}: HTTP {error.code}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    pin = pinned()
    tasks_idx, traj_idx = load_index(args.cache, pin)
    manifest = build_manifest(tasks_idx, traj_idx)
    (args.cache / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    with ThreadPoolExecutor(args.workers) as pool:
        errors = [
            e
            for e in pool.map(
                lambda r: download(r, args.cache, pin["hf_revision"]), manifest["rows"]
            )
            if e
        ]
    missing = [r["id"] for r in manifest["rows"] if not (args.cache / "files" / r["path"]).exists()]
    print(f"rows {len(manifest['rows'])}, missing {len(missing)}", file=sys.stderr)
    for line in errors[:10]:
        print(line, file=sys.stderr)
    (args.cache / "missing.json").write_text(json.dumps(sorted(missing)) + "\n")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
