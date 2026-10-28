#!/usr/bin/env python3
"""Profile local completions and cross-check six pinned public run cards.

Only aggregate counts, field names, digests and hashed task ids are written.
Run cards are fetched into memory, checked against the staged publisher
MANIFEST, reduced to metadata, and NEVER staged or copied into the checkout.
No completion is executed, exported, or sent to a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from adapter import (
    CORPUS,
    LABELS,
    MODEL,
    REPO,
    REVISION,
    ROOT,
    SHARDS,
    condition,
    hashed_id,
    iter_records,
    manifest,
    selfcheck,
)

OUTPUT = Path(__file__).resolve().parents[1] / "results/profile.json"


def distribution(values: list[int]) -> dict[str, int | float]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "min": ordered[0],
        "median": (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2,
        "max": ordered[-1],
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def publisher_cards(published: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    entries = {entry["path"]: entry for entry in published["files"]}
    cards: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    for shard in SHARDS:
        path = f"{shard}/run_card.json"
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/rollouts/mbpp/{path}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        digest = hashlib.sha256(data).hexdigest()
        checks[f"{shard}/card_digest_and_size"] = (
            len(data) == entries[path]["bytes"] and digest == entries[path]["sha256"]
        )
        if not checks[f"{shard}/card_digest_and_size"]:
            raise ValueError("public run card differs from the pinned publisher manifest")
        card = json.loads(data)
        # No prompt suffix, argv, paths, checkpoint content, or source record is emitted.
        cards[shard] = {
            "sha256": digest,
            "bytes": len(data),
            "condition": card["prompt"],
            "seed": card["seed"],
            "injected_completions": card["inject"],
            "steps_done": card["steps_done"],
            "max_steps": card["max_steps"],
            "rollout_lines_this_launch": card["rollout_lines_this_launch"],
            "rollout_calls_this_launch": card["rollout_calls_this_launch"],
            "launch_count": len(card["launches"]),
            "n_items": card["items"]["n_items"],
            "duplicated_items": card["items"]["duplicated_items"],
            "item_file_sha256": card["items"]["sha256"],
            "native_model_path_sha256": hashlib.sha256(card["model"].encode()).hexdigest(),
            "model_hub_revision_recorded": False,
            "evaluator": card["evaluator"],
            "reward_backend": card["reward_backend"],
            "record_version": card["record_version"],
            "prompt_suffix_characters": len(card["prompt_suffix"]),
            "verifier_info_in_prompt": card["verifier_info_in_prompt"],
        }
        checks[f"{shard}/uninjected_condition_seed"] = (
            card["inject"] is False
            and card["prompt"] == condition(shard)
            and card["seed"] == int(shard[-1])
            and card["reward_backend"] == "mbpp"
            and card["reward_switches"] == ""
            and card["record_version"] == "v0.2a"
            and len(card["launches"]) == 1
        )
    return cards, checks


def profile(root: Path = ROOT) -> dict[str, Any]:
    staged = manifest(root)
    published = json.loads((root / "rollouts/mbpp/MANIFEST.json").read_text(encoding="utf-8"))
    published_entries = {entry["path"]: entry for entry in published["files"]}
    verification: Counter[str] = Counter()
    files = []
    for entry in sorted(staged["files"], key=lambda item: item["path"]):
        path = root / entry["path"]
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        verification["source_size_mismatches"] += len(data) != entry["bytes"]
        verification["hub_digest_mismatches"] += (
            digest != entry["sha256"] if entry["sha256"] else blob != entry["blob_id"]
        )
        files.append({"path": entry["path"], "bytes": len(data), "sha256": digest})
        if path.name == "rollouts.jsonl.gz":
            upstream = published_entries[f"{path.parent.name}/{path.name}"]
            verification["publisher_manifest_mismatches"] += (
                digest != upstream["sha256"]
                or len(data) != upstream["bytes"]
                or digest != SHARDS[path.parent.name]
            )
    bundle_files = [path for path in root.rglob("*") if path.is_file()]
    all_paths = [root, *root.rglob("*")]
    verification["writable_paths"] = sum(bool(path.stat().st_mode & 0o222) for path in all_paths)
    expected_paths = {entry["path"] for entry in staged["files"]} | {
        "STAGE-MANIFEST.json",
        "CROSS_TEST_ONLY.md",
    }
    verification["unexpected_or_missing_bundle_files"] = len(
        {path.relative_to(root).as_posix() for path in bundle_files} ^ expected_paths
    )
    cards, checks = publisher_cards(published)
    labels = {name: Counter({"positive": 0, "negative": 0, "unlabeled": 0}) for name in LABELS}
    by_run: dict[str, Counter[str]] = defaultdict(Counter)
    by_condition: dict[str, Counter[str]] = defaultdict(Counter)
    by_task: dict[str, Counter[str]] = defaultdict(Counter)
    tasks_by_run: dict[str, set[str]] = defaultdict(set)
    steps_by_run: dict[str, Counter[int]] = defaultdict(Counter)
    fields: Counter[str] = Counter()
    rewards: Counter[str] = Counter()
    triples: Counter[str] = Counter()
    identities: list[str] = []
    trace_keys: set[str] = set()
    task_ids: set[str] = set()
    lengths: list[int] = []
    text_bytes = 0
    text_hashes: Counter[str] = Counter()
    text_gold: dict[str, set[float]] = defaultdict(set)
    hardened_only = 0
    for shard, identity, row in iter_records(root):
        identities.append(identity)
        trace_keys.add(hashed_id(identity))
        task_key = hashed_id(row["item"])
        task_ids.add(row["item"])
        tasks_by_run[shard].add(task_key)
        steps_by_run[shard][row["step"]] += 1
        fields.update(row.keys())
        grades = row.get("grades", {})
        for name in LABELS:
            value = grades.get(name)
            state = "positive" if value == 1 else "negative" if value == 0 else "unlabeled"
            labels[name][state] += 1
        state = (
            "positive"
            if grades.get("hack") == 1
            else "negative"
            if grades.get("hack") == 0
            else "unlabeled"
        )
        for counts in (by_run[shard], by_condition[condition(shard)], by_task[task_key]):
            counts["rows"] += 1
            counts[state] += 1
        triples["/".join(str(grades.get(name)) for name in LABELS)] += 1
        rewards[str(row["reward"])] += 1
        hardened_only += grades.get("hardened") == 1 and grades.get("exploitable") == 0
        encoded = row["text"].encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        text_hashes[digest] += 1
        text_gold[digest].add(grades["hack"])
        lengths.append(len(row["text"]))
        text_bytes += len(encoded)
    ids_digest = hashlib.sha256()
    for identity in sorted(identities):
        ids_digest.update(identity.encode("utf-8") + b"\n")
    adapter_check = selfcheck(root)
    for shard, card in cards.items():
        checks[f"{shard}/publisher_rows_steps_tasks"] = (
            by_run[shard]["rows"] == card["rollout_lines_this_launch"] == 4960
            and len(steps_by_run[shard])
            == card["steps_done"]
            == card["rollout_calls_this_launch"]
            == 310
            and sorted(steps_by_run[shard]) == list(range(310))
            and set(steps_by_run[shard].values()) == {16}
            and len(tasks_by_run[shard]) == card["n_items"] == 60
        )
    checks.update(
        {
            "all_local_files_verified_and_locked": not any(verification.values()),
            "six_uninjected_runs_only": len(by_run) == len(SHARDS) == 6,
            "publisher_family_has_fourteen_rollout_runs_eight_excluded": published["n_runs"] == 14,
            "publisher_row_key": published["row_key"] == "(family, run, step, idx)",
            "publisher_row_fields": set(fields) == set(published["row_fields"]),
            "publisher_total_rows": len(identities)
            == sum(card["rollout_lines_this_launch"] for card in cards.values())
            == 29760,
            "record_and_trace_ids_unique": len(identities)
            == len(set(identities))
            == len(trace_keys),
            "all_labels_binary_and_complete": all(
                counts["unlabeled"] == 0 and sum(counts.values()) == len(identities)
                for counts in labels.values()
            ),
            "same_sixty_tasks_all_runs": len(task_ids) == 60
            and len({tuple(sorted(value)) for value in tasks_by_run.values()}) == 1,
            "same_item_file_and_base_model_all_runs": len(
                {card["item_file_sha256"] for card in cards.values()}
            )
            == 1
            and len({card["native_model_path_sha256"] for card in cards.values()}) == 1,
            "adapter_zero_issues": adapter_check["issue_count"] == 0,
        }
    )
    return {
        "corpus": CORPUS,
        "revision": REVISION,
        "licence": "apache-2.0 (runs/records); cc-by-4.0 (MBPP task items)",
        "bundle_path": str(root),
        "source_files": len(files),
        "source_bytes": sum(entry["bytes"] for entry in files),
        "data_files": len(SHARDS),
        "data_bytes": sum(entry["bytes"] for entry in files if entry["path"].endswith(".jsonl.gz")),
        "bundle_files_including_local_receipts": len(bundle_files),
        "bundle_bytes_including_local_receipts": sum(path.stat().st_size for path in bundle_files),
        "rows": len(identities),
        **{state: labels["hack"][state] for state in ("positive", "negative", "unlabeled")},
        "labels": {name: dict(counts) for name, counts in labels.items()},
        "by_condition": {name: dict(counts) for name, counts in sorted(by_condition.items())},
        "by_run": {
            name: {
                **dict(counts),
                "condition": condition(name),
                "tasks": len(tasks_by_run[name]),
                "training_steps": len(steps_by_run[name]),
            }
            for name, counts in sorted(by_run.items())
        },
        "by_model_generator": {
            MODEL: {
                "rows": len(identities),
                **dict(labels["hack"]),
                "hub_checkpoint_revision_verified": False,
            }
        },
        "tasks": len(task_ids),
        "by_task_key": {key: dict(counts) for key, counts in sorted(by_task.items())},
        "task_counts_within_run": {
            name: len(tasks) for name, tasks in sorted(tasks_by_run.items())
        },
        "field_counts": dict(sorted(fields.items())),
        "reward_values": dict(sorted(rewards.items())),
        "grade_triples_exploitable_hack_hardened": dict(sorted(triples.items())),
        "hardened_pass_exploitable_fail_rows": hardened_only,
        "completion_characters": distribution(lengths),
        "completion_utf8_bytes": text_bytes,
        "distinct_completion_texts": len(text_hashes),
        "duplicate_completion_rows_beyond_first": sum(count - 1 for count in text_hashes.values()),
        "identical_text_groups_with_conflicting_hack_labels": sum(
            len(values) > 1 for values in text_gold.values()
        ),
        "distinct_record_ids": len(set(identities)),
        "distinct_trace_keys": len(trace_keys),
        "sorted_record_ids_sha256": ids_digest.hexdigest(),
        "sorted_record_ids_encoding": "compact JSON [family,run,step,idx], lexicographically sorted, UTF-8 with LF after each id",
        "publisher_run_cards": cards,
        "publisher_label_totals_available": False,
        "publisher_label_total_limit": "MANIFEST publishes sizes/digests, not label totals. Run cards publish row/step/item counts, not grade totals. Grade totals are computed from every digest-verified row, not independently publisher-validated.",
        "verification": dict(sorted(verification.items())),
        "checks": dict(sorted(checks.items())),
        "all_checks_passed": all(checks.values()),
        "adapter_selfcheck": adapter_check,
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "rows",
                    "positive",
                    "negative",
                    "unlabeled",
                    "by_condition",
                    "tasks",
                    "source_files",
                    "source_bytes",
                    "data_bytes",
                    "sorted_record_ids_sha256",
                    "all_checks_passed",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )
    raise SystemExit(not result["all_checks_passed"])


if __name__ == "__main__":
    main()
