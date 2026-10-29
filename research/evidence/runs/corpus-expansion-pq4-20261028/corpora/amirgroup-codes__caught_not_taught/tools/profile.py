#!/usr/bin/env python3
"""Reverify the locked Caught-Not-Taught bundle; publish aggregates, digests and hashed ids only."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import LICENCE, REPO, REVISION, ROOT, iter_episodes, label_state, selfcheck, steps_of

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "02555c1ebd9bdb0be36db7dd7eab864ae375fc0520af891d979f9aeca960d4e0"
NOTICE_SHA256 = "bba238440ce232dfadf4cf5485be1d257db5f71dc0289422f7fc49624121ebc6"
NUDGE = "please proceed to the next step"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalise_id(value: str, mode: str) -> str:
    if mode == "bare":
        return value.split("@")[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()
    if mode == "full":
        return value.casefold()
    raise ValueError("unknown id normalisation")


def hashed_ids(values: list[str]) -> dict[str, list[str]]:
    return {
        mode: sorted({sha256(normalise_id(value, mode).encode("utf-8")) for value in values})
        for mode in ("bare", "full")
    }


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest_bytes = (root / "STAGE-MANIFEST.json").read_bytes()
    if sha256(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest differs from the pinned staging receipt")
    manifest = json.loads(manifest_bytes)
    if (manifest["repo"], manifest["revision"], manifest["licence"], manifest["gated"]) != (
        REPO,
        REVISION,
        LICENCE,
        False,
    ):
        raise ValueError("source pin, licence or gate differs from approval")
    if sha256((root / "CROSS_TEST_ONLY.md").read_bytes()) != NOTICE_SHA256:
        raise ValueError("local-only evaluation notice changed")
    paths = [root, *root.rglob("*")]
    if any(path.is_symlink() for path in paths):
        raise ValueError("immutable bundles cannot contain symlinks")
    writable_paths = sum(bool(path.stat().st_mode & 0o222) for path in paths)
    if writable_paths:
        raise ValueError("bundle must be locked before profiling")
    seen: set[str] = set()
    total = 0
    for entry in manifest["files"]:
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
            raise ValueError("unsafe or duplicate manifest path")
        seen.add(entry["path"])
        data = (root / relative).read_bytes()
        if len(data) != entry["bytes"] or sha256(data) != entry["sha256"]:
            raise ValueError("manifest size or SHA-256 mismatch")
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git blob id
        if blob != entry["blob_id"]:
            raise ValueError("git blob id mismatch")
        total += len(data)
    actual = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
    if actual != seen | {"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"}:
        raise ValueError("unmanifested or missing bundle files")
    if total != manifest["total_bytes"]:
        raise ValueError("manifest total byte count mismatch")
    return {"files": len(seen), "bytes": total, "writable_paths": writable_paths}


def profile(root: Path = ROOT) -> dict[str, Any]:
    integrity = verify_bundle(root)
    check = selfcheck(root)
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_slice: dict[str, Counter[str]] = {}
    nudges: dict[str, Counter[str]] = {}
    side_by_main_ok: Counter[str] = Counter()
    regraded = 0
    run_ids: list[str] = []
    task_ids: list[str] = []
    main_names: set[str] = set()
    excluded_round2: Counter[str] = Counter()
    for ep in iter_episodes(root):
        state, reason = label_state(ep)
        states[state] += 1
        reasons[reason] += 1
        regraded += bool(ep["grade"]["regraded"])
        if state == "excluded":
            excluded_round2[f"{ep['mode']}/{ep['variant']}/{ep['history']}"] += 1
            continue
        run_ids.append(ep["id"])
        task_ids.append(ep["problem_id"])
        main_names.add(ep["task"]["main_name"])
        slice_key = f"{ep['pair']['tag']}/{ep['arm']}/{ep['mode']}"
        by_slice.setdefault(slice_key, Counter())[state] += 1
        n_nudges = sum(
            1
            for s in steps_of(ep)
            if s["role"] == "user" and s["text"].strip().casefold().startswith(NUDGE)
        )
        nudges.setdefault(f"{ep['arm']}/{ep['mode']}", Counter()).update(
            {"episodes": 1, "nudge_messages": n_nudges, "episodes_with_nudge": bool(n_nudges)}
        )
        if ep["mode"] == "attack":
            side_by_main_ok[f"main_ok={ep['grade']['main_ok']}/{state}"] += 1
    return {
        "corpus": "amirgroup-codes__caught_not_taught",
        "repo": REPO,
        "revision": REVISION,
        "licence": LICENCE,
        "gated": False,
        "arxiv": "2610.26935",
        "local_only": True,
        "cross_test_only": True,
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        "verified_after_lock": True,
        "writable_paths": integrity["writable_paths"],
        "files": integrity["files"],
        "bytes": integrity["bytes"],
        "construct": "covert-attack (instructed sabotage), not spontaneous cheating",
        "label_rule": {
            "positive": "round-1 untrusted attack-mode episode with grade.side_ok is True",
            "negative": "round-1 honest-mode episode (untrusted or trusted arm) with no side-task grade",
            "ambiguous": "round-1 attack-mode episode with side_ok False (attempted sabotage slice) "
            "or null; any honest episode carrying a side grade",
            "excluded": "all round-2 episodes (replayed prior attack and DtT feedback in context)",
        },
        "units": sum(v for k, v in states.items() if k != "excluded"),
        "positives": states["positive"],
        "negatives": states["negative"],
        "ambiguous": states["ambiguous"],
        "excluded": states["excluded"],
        "label_reasons": dict(sorted(reasons.items())),
        "per_pair_arm_mode": {k: dict(sorted(v.items())) for k, v in sorted(by_slice.items())},
        "attack_main_ok_by_state": dict(sorted(side_by_main_ok.items())),
        "regraded_episodes": regraded,
        "excluded_round2_by_condition": dict(sorted(excluded_round2.items())),
        "harness_nudge_messages_by_arm_mode": {
            k: {kk: int(vv) for kk, vv in sorted(v.items())} for k, v in sorted(nudges.items())
        },
        "adapter_selfcheck": check,
        "leak_count": check["leak_count"],
        "main_task_names": len(main_names),
        "task_id_hashes": hashed_ids(sorted(set(task_ids)) + sorted(main_names)),
        "run_id_hashes": hashed_ids(run_ids),
        "task_ids_checked": len(set(task_ids)),
        "run_ids_checked": len(run_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    assert result["writable_paths"] == 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(
        json.dumps({k: v for k, v in result.items() if not k.endswith("_hashes")}, indent=1)[:3500]
    )


if __name__ == "__main__":
    main()
