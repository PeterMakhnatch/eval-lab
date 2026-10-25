#!/usr/bin/env python3
"""Exact pinned byte staging for the nine Goodfire judge runs (no JSON decoding).

Every file is streamed from the immutable HF revision into a temporary file,
checked against the retained tree listing (size plus LFS sha256 or git-blob
sha1), then hard-linked into the local CAS (cas/sha256/<digest>) and into
goodfire/raw/<revision>/<path>. A mismatch deletes the temporary file and is
recorded as a failure; nothing is retried under a different identity.
Existing verified files are re-hashed, never re-downloaded.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "Goodfire/reward-hack-data"
REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
TREE = Path("/Users/petermakhnatch/Developer/research-context/reward-hacking/datasets/raw/"
            "goodfire-reward-hack-data/tree.json")
JUDGE_RUNS = (
    "swebench-verified-glm-5.2-nohack", "swebench-verified-kimi-k3-nohack",
    "swebench-verified-qwen3.8-2.4t-a95b-fp8-nohack", "deepswe-glm-5.2-fp8", "deepswe-kimi-k3",
    "deepswe-qwen3.8-2.4t-a95b-fp8", "impossiblebench-glm-5.2-fp8", "impossiblebench-kimi-k3",
    "impossiblebench-qwen3.8-2.4t-a95b-fp8",
)
STREAMS = ("traces.jsonl", "labels/judge_passages.jsonl")
CHUNK = 1 << 20
UA = {"User-Agent": "evallab-lb1-goodfire-stage/1", "Accept-Encoding": "identity"}


def selected(entries, mode):
    for entry in entries:
        if entry["type"] != "file":
            continue
        path = entry["path"]
        if path.split("/", 1)[0] not in JUDGE_RUNS:
            continue
        is_stream = path.endswith(STREAMS)
        if mode == "metadata" and is_stream:
            continue
        if mode == "streams" and not is_stream:
            continue
        yield entry


def verify_file(path: Path, entry):
    sha256 = hashlib.sha256()
    size = path.stat().st_size
    sha1 = hashlib.sha1(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            sha256.update(chunk)
            sha1.update(chunk)
    return size, sha256.hexdigest(), sha1.hexdigest()


def check(entry, size, sha256, sha1):
    if size != entry["size"]:
        return f"size {size} != tree {entry['size']}"
    lfs = entry.get("lfs")
    if lfs:
        if sha256 != lfs["oid"]:
            return "sha256 != tree lfs oid"
    elif sha1 != entry["oid"]:
        return "git blob sha1 != tree oid"
    return None


def link(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if os.stat(destination).st_ino != os.stat(source).st_ino:
            raise RuntimeError(f"refusing to replace existing {destination}")
        return
    os.link(source, destination)


def fetch(entry, out: Path):
    path = entry["path"]
    raw = out / "goodfire" / "raw" / REVISION / path
    started = time.time()
    receipt = {"path": path, "tree_size": entry["size"], "tree_git_oid": entry["oid"],
               "tree_lfs_sha256": (entry.get("lfs") or {}).get("oid"), "storage": "lfs" if entry.get("lfs") else "git"}
    if raw.exists():
        size, sha256, sha1 = verify_file(raw, entry)
        receipt.update(origin="existing_verified_local", bytes_downloaded=0)
    else:
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{urllib.parse.quote(path)}"
        tmpdir = out / "goodfire" / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=tmpdir)
        tmp = Path(tmp_name)
        sha256_h = hashlib.sha256()
        sha1_h = hashlib.sha1(f"blob {entry['size']}\0".encode())
        size = 0
        try:
            with os.fdopen(fd, "wb") as sink, urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=120
            ) as response:
                final_host = urllib.parse.urlparse(response.geturl()).hostname or ""
                if not (final_host == "huggingface.co" or final_host.endswith((".huggingface.co", ".hf.co"))):
                    raise RuntimeError(f"unexpected final host {final_host}")
                while chunk := response.read(CHUNK):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise RuntimeError("response exceeds pinned size")
                    sha256_h.update(chunk)
                    sha1_h.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            sha256, sha1 = sha256_h.hexdigest(), sha1_h.hexdigest()
            problem = check(entry, size, sha256, sha1)
            if problem:
                raise RuntimeError(problem)
            cas = out / "cas" / "sha256" / sha256
            cas.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(tmp, cas)
            except FileExistsError:  # identical bytes already staged by another file
                pass
            tmp.unlink()
            link(cas, raw)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        receipt.update(origin="downloaded_pinned_revision", bytes_downloaded=size, final_host=final_host)
    problem = check(entry, size, sha256, sha1)
    if problem:
        raise RuntimeError(f"{path}: {problem}")
    cas = out / "cas" / "sha256" / sha256
    if not cas.exists():
        try:
            os.link(raw, cas)
        except FileExistsError:
            pass
    if verify_file(cas, entry)[1] != sha256:
        raise RuntimeError(f"{path}: CAS object differs")
    receipt.update(size=size, sha256=sha256, git_blob_sha1=sha1, verified_against_tree=True,
                   cas_uri=f"cas://sha256/{sha256}", seconds=round(time.time() - started, 3))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("metadata", "streams", "all"), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    entries = sorted(selected(json.loads(TREE.read_text()), args.mode), key=lambda e: -e["size"])
    receipts, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(fetch, entry, args.out): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                receipts.append(future.result())
            except Exception as error:  # recorded, never silently skipped
                failures.append({"path": entry["path"], "error": repr(error)})
    receipts.sort(key=lambda r: r["path"])
    summary = {
        "schema": "lb1.goodfire.fetch_receipt/1", "repo": REPO, "revision": REVISION,
        "tree_listing": str(TREE), "tree_listing_sha256": hashlib.sha256(TREE.read_bytes()).hexdigest(),
        "mode": args.mode, "files_selected": len(entries), "files_verified": len(receipts),
        "failures": failures, "bytes_verified": sum(r["size"] for r in receipts),
        "bytes_downloaded": sum(r["bytes_downloaded"] for r in receipts), "json_decoded": False,
        "files": receipts,
    }
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=1, sort_keys=True)
    print(json.dumps({k: v for k, v in summary.items() if k != "files"}, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
