#!/usr/bin/env python3
"""Exact pinned fetch of Terminal Wrench payload files for allowlist-matched tasks only.

Task selection is the exact native task_id == bare canonical DEV name in the
parent allowlist (sha256 pinned). Every file under tasks/<task_id>/ at the
pinned payload revision is listed via the HF tree API, downloaded, checked
against its listed size and LFS sha256 / git blob sha1, then hard-linked into
cas/sha256/<digest> and wrench/raw-v3/<revision>/<path>.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1])
ALLOWLIST = Path("/private/tmp/lb1-20261025/preexisting-dev-split-allowlist.json")
ALLOWLIST_SHA256 = "c82b251f5c5bb51466b154222e3cec3bb7cbd389768ce1e79cf17d55f63dc8fa"
REPO = "few-sh/terminal-wrench"
REV = "6ab64c43a4c6a4d9aa53c2425f37e715257123a3"
UA = {"User-Agent": "evallab-lb1-wrench-dev-subset/3", "Accept-Encoding": "identity"}


def bare(name: str) -> str:
    return name.split("@", 1)[0].rsplit("/", 1)[-1]


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as response:
        return json.load(response)


def fetch(entry: dict) -> dict:
    path = entry["path"]
    raw = OUT / "wrench" / "raw-v3" / REV / path
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{REV}/{urllib.parse.quote(path)}"
    tmpdir = OUT / "wrench" / "tmp-v3"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmpdir)
    tmp = Path(name)
    h256, h1, size = hashlib.sha256(), hashlib.sha1(f"blob {entry['size']}\0".encode()), 0
    try:
        with os.fdopen(fd, "wb") as sink, urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as response:
            host = urllib.parse.urlparse(response.geturl()).hostname or ""
            if not (host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co"))):
                raise RuntimeError(f"unexpected host {host}")
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > entry["size"]:
                    raise RuntimeError("larger than listed size")
                h256.update(chunk)
                h1.update(chunk)
                sink.write(chunk)
        digest = h256.hexdigest()
        lfs = entry.get("lfs")
        if size != entry["size"] or (lfs and digest != lfs["oid"]) or (not lfs and h1.hexdigest() != entry["oid"]):
            raise RuntimeError("size/hash differs from pinned tree listing")
        cas = OUT / "cas" / "sha256" / digest
        cas.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(tmp, cas)
        except FileExistsError:
            if hashlib.sha256(cas.read_bytes()).hexdigest() != digest:
                raise RuntimeError("existing CAS object corrupt")
        raw.parent.mkdir(parents=True, exist_ok=True)
        os.link(cas, raw)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": path, "size": size, "sha256": digest, "git_blob_sha1": h1.hexdigest(),
            "storage": "lfs" if entry.get("lfs") else "git", "cas_uri": f"cas://sha256/{digest}",
            "verified_against_tree_listing": True}


def main() -> int:
    data = ALLOWLIST.read_bytes()
    if hashlib.sha256(data).hexdigest() != ALLOWLIST_SHA256:
        raise SystemExit("allowlist digest mismatch")
    allow = {bare(row["task_id"]): row for row in json.loads(data)["rows"]}
    tasks = [json.loads(line) for line in (OUT / "wrench" / "alias-manifest.jsonl").open()]
    matched = sorted(t["native_task_id"] for t in tasks if bare(t["native_task_id"]) in allow)
    listing, receipts, failures = {}, [], []
    for task_id in matched:
        url = f"https://huggingface.co/api/datasets/{REPO}/tree/{REV}/tasks/{urllib.parse.quote(task_id)}?recursive=true"
        listing[task_id] = get_json(url)
    with (OUT / "wrench" / "payload-tree-listing-v3.json").open("x") as stream:
        json.dump({"repo": REPO, "revision": REV, "listings": listing}, stream, indent=1, sort_keys=True)
    files = [e for entries in listing.values() for e in entries if e["type"] == "file"]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = {pool.submit(fetch, e): e for e in files}
        for future in concurrent.futures.as_completed(futures):
            try:
                receipts.append(future.result())
            except Exception as error:
                failures.append({"path": futures[future]["path"], "error": repr(error)})
    receipts.sort(key=lambda r: r["path"])
    summary = {"schema": "lb1.wrench.dev_subset_fetch/3", "repo": REPO, "revision": REV,
               "allowlist": str(ALLOWLIST), "allowlist_sha256": ALLOWLIST_SHA256,
               "matched_tasks": [{"native_task_id": t, "allowlist_row": allow[bare(t)], "match": "exact native task_id == bare canonical name"} for t in matched],
               "unmatched_tasks_held_before_body_fetch": len(tasks) - len(matched),
               "files_listed": len(files), "files_verified": len(receipts), "failures": failures,
               "bytes_verified": sum(r["size"] for r in receipts), "files": receipts}
    with (OUT / "wrench-fetch-dev-subset-v3.json").open("x") as stream:
        json.dump(summary, stream, indent=1, sort_keys=True)
    print(json.dumps({k: v for k, v in summary.items() if k != "files"}, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
