#!/usr/bin/env python3
"""Record tar-member mtimes for one Xiaomi image without materializing it.

Reuses :class:`Registry` from the HAR-177 leak scan to stream each layer and
iterate tar headers only: member names, mtimes and sizes are recorded, no file
bytes are written to disk. Output is a JSON receipt with the per-root mtime
histogram summary plus the files standing at the latest mtime (the
fix-commit-mtime leak shape on 002402).

$0: registry streaming only. No model, Modal, or Daytona use.
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import sys
import tarfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "har177-leak-scan"))
from scan import Registry  # noqa: E402

TOP_N = 25

def clean_name(name: str) -> str:
    while name.startswith("./"):
        name = name[len("./") :]
    return name.lstrip("/")

def scan_blob(
    registry: Registry, blob: str, *, detail: set[str]
) -> tuple[dict[str, Counter], dict[str, list], dict[str, dict], str, int]:
    """Stream one layer.

    Returns ``(histograms, latest-heaps, detail-paths, method, members)``.
    ``detail-paths`` maps a detail root to ``{mtime: [(size, rel)]}`` with
    every member path (detail roots are small worktrees, not system dirs).
    """
    stream, method = registry.open_blob(blob)
    hists: dict[str, Counter] = {}
    latest: dict[str, list] = {}
    paths: dict[str, dict] = {}
    members = 0
    try:
        with gzip.GzipFile(fileobj=stream) as gunzip, tarfile.open(
            fileobj=gunzip, mode="r|*"
        ) as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = clean_name(member.name)
                if not name or "/" not in name:
                    continue
                root, rel = name.split("/", 1)
                hists.setdefault(root, Counter())[member.mtime] += 1
                heap = latest.setdefault(root, [])
                entry = (member.mtime, member.size, rel)
                if len(heap) < TOP_N:
                    heapq.heappush(heap, entry)
                elif entry > heap[0]:
                    heapq.heapreplace(heap, entry)
                if root in detail:
                    paths.setdefault(root, {}).setdefault(member.mtime, []).append(
                        (member.size, rel)
                    )
                members += 1
    finally:
        stream.close()
    return hists, latest, paths, method, members


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True, help="image digest sha256:...")
    parser.add_argument("--task", required=True, help="task id for the receipt")
    parser.add_argument("--out", required=True, help="receipt JSON path")
    parser.add_argument("--roots", default="testbed", help="comma roots for detail")
    args = parser.parse_args()

    registry = Registry()
    manifest, manifest_method = registry.manifest(args.digest)
    layers = manifest["layers"]
    merged: dict[str, Counter] = {}
    merged_latest: dict[str, list] = {}
    layer_rows = []
    total = 0
    detail = set(args.roots.split(","))
    merged_paths: dict[str, dict] = {}
    for digest in layers:
        hists, latest, paths, method, members = scan_blob(registry, digest, detail=detail)
        total += members
        for root, hist in hists.items():
            merged.setdefault(root, Counter()).update(hist)
        for root, heap in latest.items():
            dest = merged_latest.setdefault(root, [])
            for entry in heap:
                if len(dest) < TOP_N:
                    heapq.heappush(dest, entry)
                elif entry > dest[0]:
                    heapq.heapreplace(dest, entry)
        for root, by_mtime in paths.items():
            dest = merged_paths.setdefault(root, {})
            for mtime, entries in by_mtime.items():
                dest.setdefault(mtime, []).extend(entries)
        layer_rows.append(
            {"digest": digest, "method": method, "members": members, "roots": sorted(hists)}
        )
        print(f"layer {digest[:19]} members={members} roots={sorted(hists)[:6]}", flush=True)

    summary = {}
    for root, hist in merged.items():
        row: dict = {
            "members": sum(hist.values()),
            "distinct_mtimes": len(hist),
            "bulk_mtime": hist.most_common(1)[0][0],
            "bulk_iso": _iso(hist.most_common(1)[0][0]),
            "max_mtime": max(hist),
            "max_iso": _iso(max(hist)),
        }
        if root in detail:
            top = sorted(merged_latest[root], reverse=True)[:TOP_N]
            row["latest_overall"] = [
                {"mtime": m, "iso": _iso(m), "size": s, "path": p} for (m, s, p) in top
            ]
            row["files_at_max_mtime"] = sorted(p for (m, _s, p) in top if m == max(hist))
            by_mtime = merged_paths.get(root, {})
            row["histogram"] = [
                {
                    "mtime": m,
                    "iso": _iso(m),
                    "count": hist[m],
                    "paths": sorted(p for (_s, p) in by_mtime.get(m, [])),
                }
                for m in sorted(hist)
            ]
        summary[root] = row

    receipt = {
        "task": args.task,
        "image_digest": args.digest,
        "manifest_method": manifest_method,
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_members": total,
        "layers": layer_rows,
        "roots": summary,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"members={total} receipt={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
