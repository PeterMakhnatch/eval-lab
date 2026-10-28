#!/usr/bin/env python3
"""Reverify the locked BAITBENCH bundle; publish aggregates, digests and hashed ids only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from adapter import (
    CANONICAL,
    CORPUS,
    REPO,
    REVISION,
    ROOT,
    TASK_NAMES,
    family_id,
    human,
    instance_id,
    iter_rows,
    judgments,
    label_state,
    prompt_group,
    selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"
MANIFEST_SHA256 = "b5cf12f2a6b4a13122f23d9a6cad1dad2b044da11d536ef93a82f71f761cc5d0"
NOTICE_SHA256 = "599b5a309c9ca4b6db44971caf4cf7c95c1daeb8228c8fddf25492faddc11440"
CANONICAL_SHA256 = "6e382e83faba05f9c31cf773f2a1e5f990cf0c99aec0d7bead3ececf796ca1a3"
RECEIPTS = frozenset({"STAGE-MANIFEST.json", "CROSS_TEST_ONLY.md"})


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
        mode: sorted(sha256(normalise_id(value, mode).encode("utf-8")) for value in values)
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
        "MIT",
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
    inventory = []
    seen: set[str] = set()
    for index, entry in enumerate(sorted(manifest["files"], key=lambda item: item["path"])):
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
            raise ValueError("unsafe or duplicate manifest path")
        seen.add(entry["path"])
        path = root / relative
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("missing or wrong-sized manifest file")
        data = path.read_bytes()
        digest = sha256(data)
        if digest != entry["sha256"] or digest != entry["sha256_computed"]:
            raise ValueError("manifest final-byte SHA-256 mismatch")
        if entry["verified_against"] != "github_git_blob_sha1":
            raise ValueError("unrecognized publisher digest method")
        if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != entry["blob_id"]:
            raise ValueError("publisher Git blob mismatch")
        inventory.append(
            {
                "file_index": index,
                "path_sha256": sha256(entry["path"].encode("utf-8")),
                "bytes": len(data),
                "sha256": digest,
                "publisher_blob_id": entry["blob_id"],
                "verified_against": entry["verified_against"],
            }
        )
    canonical = next(f for f in inventory if f["path_sha256"] == sha256(CANONICAL.encode("utf-8")))
    if canonical["sha256"] != CANONICAL_SHA256:
        raise ValueError("canonical dataset digest differs from the publisher's DATA_MANIFEST.md")
    actual_files = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
    if actual_files != seen | RECEIPTS:
        raise ValueError("unmanifested or missing bundle files")
    payload_bytes = sum(entry["bytes"] for entry in inventory)
    if payload_bytes != manifest["total_bytes"]:
        raise ValueError("manifest total byte count mismatch")
    return {
        "files": len(inventory),
        "bytes": payload_bytes,
        "filesystem_files_including_receipts": len(actual_files),
        "filesystem_bytes_including_receipts": sum(
            path.stat().st_size for path in paths if path.is_file()
        ),
        "publisher_git_blob_verified_files": len(inventory),
        "canonical_sha256_verified": True,
        "verified_after_lock": True,
        "writable_paths": writable_paths,
        "file_digests": inventory,
    }


def _tally(
    table: dict[str, Counter[str]], key: str, state: str, rh_pair: str | None = None
) -> None:
    table[key]["rows"] += 1
    table[key][state] += 1
    if rh_pair:
        table[key][rh_pair] += 1


def profile(root: Path = ROOT) -> dict[str, Any]:
    integrity = verify_bundle(root)
    checks = selfcheck(root)
    judged = judgments(root)
    annotated = human(root)
    rows = list(iter_rows(root))
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    tables: dict[str, dict[str, Counter[str]]] = {
        name: defaultdict(Counter)
        for name in (
            "task",
            "model",
            "prompt_condition",
            "prompt_group",
            "task_x_model",
            "task_x_prompt_group",
            "model_x_prompt_condition",
            "instance",
            "family_key",
        )
    }
    pairs: Counter[str] = Counter()
    neutral: Counter[str] = Counter()
    neutral_vs_state: Counter[str] = Counter()
    human_vs_state: Counter[str] = Counter()
    gap: dict[str, list[float]] = defaultdict(list)
    gap_status: Counter[str] = Counter()
    run_ids = []
    for row in rows:
        meta = row["metadata"]
        run_id = row["run_id"]
        state, reason = label_state(row, judged)
        states[state] += 1
        reasons[reason] += 1
        claude = judged["claude"].get(run_id, {}).get("label", "missing")
        gpt = judged["gpt"].get(run_id, {}).get("label", "missing")
        pairs[f"claude={claude}|gpt={gpt}"] += 1
        glm = judged["neutral"].get(run_id, {}).get("label", "missing")
        neutral[glm] += 1
        neutral_vs_state[f"{state}|glm={glm}"] += 1
        verdict = annotated.get(run_id)
        if verdict:
            human_vs_state[f"{state}|human={verdict}"] += 1
        task = TASK_NAMES[meta["family_code"]]
        group = prompt_group(run_id)
        run_ids.append(run_id)
        for table, key in (
            ("task", task),
            ("model", meta["model"]),
            ("prompt_condition", meta["prompt_condition"]),
            ("prompt_group", group),
            ("task_x_model", f"{task} | {meta['model']}"),
            ("task_x_prompt_group", f"{task} | {group}"),
            ("model_x_prompt_condition", f"{meta['model']} | {meta['prompt_condition']}"),
            ("instance", digest_key(instance_id(meta["task_id"]))),
            ("family_key", digest_key(family_id(meta["task_id"]))),
        ):
            _tally(tables[table], key, state)
        value = row["derived_signals"]["generalization_gap"]
        finite = isinstance(value, float) and math.isfinite(value)
        kind = "finite_gap" if finite else "nonfinite_gap" if isinstance(value, float) else "no_gap"
        gap_status[f"{state}|{kind}"] += 1
        if finite:
            gap[state].append(value)
    task_ids = sorted({r["metadata"]["task_id"] for r in rows})
    instance_ids = sorted({instance_id(t) for t in task_ids})
    family_ids = sorted({family_id(t) for t in task_ids})
    run_hashes = hashed_ids(run_ids)
    task_hashes = hashed_ids(task_ids)
    instance_hashes = hashed_ids(instance_ids)
    family_hashes = hashed_ids(family_ids)
    scoreable = states["positive"] + states["negative"]
    provided = {
        "rows": 1258,
        "consensus_reward_hacking_before_exclusion": 681,
        "consensus_not_reward_hacking_before_exclusion": 488,
        "neutral_reward_hacking": 749,
        "neutral_not_reward_hacking": 497,
        "neutral_unclear": 12,
        "tasks_entity_overlap": 418,
        "tasks_near_duplicate_leakage": 420,
        "tasks_no_signal_classification": 420,
        "model_kimi_k2_5": 178,
        "prompt_baseline": 628,
        "prompt_validity": 630,
    }
    observed = {
        "rows": len(rows),
        "consensus_reward_hacking_before_exclusion": pairs[
            "claude=reward_hacking|gpt=reward_hacking"
        ],
        "consensus_not_reward_hacking_before_exclusion": pairs[
            "claude=not_reward_hacking|gpt=not_reward_hacking"
        ],
        "neutral_reward_hacking": neutral["reward_hacking"],
        "neutral_not_reward_hacking": neutral["not_reward_hacking"],
        "neutral_unclear": neutral["unclear"],
        "tasks_entity_overlap": tables["task"]["Entity overlap"]["rows"],
        "tasks_near_duplicate_leakage": tables["task"]["Near-duplicate leakage"]["rows"],
        "tasks_no_signal_classification": tables["task"]["No-signal classification"]["rows"],
        "model_kimi_k2_5": tables["model"]["kimi-k2.5"]["rows"],
        "prompt_baseline": tables["prompt_group"]["baseline"]["rows"],
        "prompt_validity": tables["prompt_group"]["validity"]["rows"],
    }

    def summary(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "mean": round(statistics.fmean(values), 6),
            "median": round(statistics.median(values), 6),
        }

    def render(table: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
        return {key: dict(value) for key, value in sorted(table.items())}

    return {
        "corpus": CORPUS,
        "repo": REPO,
        "revision": REVISION,
        "licence": "MIT",
        "gated": False,
        "local_only": True,
        "cross_test_only": True,
        "manifest_sha256": MANIFEST_SHA256,
        "notice_sha256": NOTICE_SHA256,
        **integrity,
        "rows": len(rows),
        "positives": states["positive"],
        "negatives": states["negative"],
        "ambiguous": states["ambiguous"],
        "excluded": states["excluded"],
        "scoreable": scoreable,
        "label_states": dict(states),
        "label_reasons": dict(reasons),
        "excluded_by_reason": {k: v for k, v in reasons.items() if k.startswith("no_agent")},
        "ambiguous_by_reason": {
            k: v for k, v in reasons.items() if k.startswith(("original", "missing"))
        },
        "original_judge_pairs_all_rows": dict(sorted(pairs.items())),
        "neutral_glm_labels_all_rows": dict(sorted(neutral.items())),
        "neutral_glm_vs_label_state": dict(sorted(neutral_vs_state.items())),
        "human_v4_verdicts_vs_label_state": dict(sorted(human_vs_state.items())),
        "generalization_gap_by_state": {
            "availability": dict(sorted(gap_status.items())),
            **{state: summary(values) for state, values in sorted(gap.items())},
        },
        "per_task": render(tables["task"]),
        "per_model": render(tables["model"]),
        "per_prompt_condition": render(tables["prompt_condition"]),
        "per_prompt_group": render(tables["prompt_group"]),
        "per_task_x_model": render(tables["task_x_model"]),
        "per_task_x_prompt_group": render(tables["task_x_prompt_group"]),
        "per_model_x_prompt_condition": render(tables["model_x_prompt_condition"]),
        "per_dataset_instance_sha256": render(tables["instance"]),
        "per_task_family_sha256": render(tables["family_key"]),
        "task_id_hashes": task_hashes,
        "instance_id_hashes": instance_hashes,
        "family_id_hashes": family_hashes,
        "run_id_hashes": run_hashes,
        "id_uniqueness": {
            "run_ids": len(set(run_ids)),
            "native_task_ids": len(task_ids),
            "dataset_instances": len(instance_ids),
            "task_families": len(family_ids),
            "run_normalised_unique_hashes": {m: len(set(v)) for m, v in run_hashes.items()},
            "task_normalised_unique_hashes": {m: len(set(v)) for m, v in task_hashes.items()},
        },
        "bootstrap": {
            "resamples": 10000,
            "seed": 0,
            "by_task_family": {"clusters": len(family_ids)},
            "by_task_x_model": {"clusters": len(tables["task_x_model"])},
            "by_dataset_instance": {"clusters": len(instance_ids)},
        },
        "provided_count_checks": {
            key: {"provided": value, "observed": observed[key], "difference": observed[key] - value}
            for key, value in provided.items()
        },
        "adapter_selfcheck": checks,
    }


def digest_key(value: str) -> str:
    return sha256(value.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "files",
                    "bytes",
                    "rows",
                    "positives",
                    "negatives",
                    "ambiguous",
                    "excluded",
                    "scoreable",
                    "manifest_sha256",
                    "verified_after_lock",
                    "writable_paths",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
