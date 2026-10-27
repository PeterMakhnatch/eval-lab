#!/usr/bin/env python3
"""Read the local Apollo bundle without exporting text or leaking gold into steps.

This is response-level deception/misalignment evidence, not reward-hacking gold.
Only prompt_dialogue and response enter the detector-facing ATIF-style steps.
All commands are deterministic; no model, database, CAS, or network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "apolloresearch__insider-trading"
REVISION = "370fdc9f303f972fe3d2e40b71d06bc04027668b"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
LABELS = ("is_misaligned", "is_deceptive", "is_doubling_down")
PHASE_LABEL = dict(zip(("misalignment", "deception", "doubling_down"), LABELS, strict=True))
EXPECTED_PHASE_COUNTS = {"misalignment": 9600, "deception": 4255, "doubling_down": 568}
EXPECTED_FILES = 14468
EXPECTED_BYTES = 114160984
EXPECTED_RESULT_BYTES = 112184117


def hashed_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def manifest(root: Path = ROOT) -> dict[str, Any]:
    result = json.loads((root / "STAGE-MANIFEST.json").read_text(encoding="utf-8"))
    if result["revision"] != REVISION or result["licence"] != "CC-BY-4.0":
        raise ValueError("bundle revision or licence does not match the pinned contract")
    return result


def result_entries(root: Path = ROOT) -> list[dict[str, Any]]:
    return sorted(
        (
            entry
            for entry in manifest(root)["files"]
            if entry["path"].startswith("results/") and entry["path"].endswith(".json")
        ),
        key=lambda entry: entry["path"],
    )


def iter_records(root: Path = ROOT) -> Iterator[tuple[str, dict[str, Any]]]:
    """Record ids are pinned, POSIX-relative file paths; never print or persist them."""
    for entry in result_entries(root):
        yield entry["path"], json.loads((root / entry["path"]).read_text(encoding="utf-8"))


def metadata(record_id: str) -> dict[str, str]:
    parts = Path(record_id).parts
    phase = parts[-2]
    if parts[0] != "results" or phase not in PHASE_LABEL or len(parts) not in (5, 6):
        raise ValueError("unexpected source path structure")
    # Remove ONLY the phase component. Response numbers repeat across model/scenario
    # directories, so a bare basename would incorrectly merge unrelated runs.
    cluster_id = "/".join((*parts[:-2], parts[-1]))
    task_id = "/".join((parts[1], *parts[3:-2]))
    return {
        "phase": phase,
        "target_label": PHASE_LABEL[phase],
        "experiment": parts[1],
        "cluster_key": hashed_id(cluster_id),
        "task_key": hashed_id(task_id),
    }


def detector_input(row: dict[str, Any]) -> dict[str, Any]:
    """The complete allowlist. Nothing from filenames, labels, or metadata is included."""
    return {"prompt_dialogue": row["prompt_dialogue"], "response": row["response"]}


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve native message boundaries, roles, order and every text character.

    ATIF-style source has no system variant: map system to user while retaining
    original_role. The last assistant step is the scored continuation. No tools,
    observations, or private reasoning are invented from unstructured text.
    """
    allowed = detector_input(row)
    messages = [*allowed["prompt_dialogue"], {"role": "assistant", "content": allowed["response"]}]
    return [
        {
            "step_id": index,
            "source": "agent" if message["role"] == "assistant" else "user",
            "original_role": message["role"],
            "message": message["content"],
        }
        for index, message in enumerate(messages)
    ]


def specs(label: str | None = None, root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield one spec per native response, or only the requested label's phase.

    Pass ONLY _steps (or detector_input(row)) to a frozen monitor. Everything
    else is scorer-only. trace_id is in-memory join state, never receipt content.
    """
    if label is not None and label not in LABELS:
        raise ValueError("unknown native label")
    for record_id, row in iter_records(root):
        meta = metadata(record_id)
        target = meta["target_label"]
        if label is not None and target != label:
            continue
        yield {
            "corpus": CORPUS,
            "trace_id": record_id,
            "trace_key": hashed_id(record_id),
            **meta,
            "model": row["model"],
            "positive": row[target],
            "gold_labels": {key: row[key] for key in LABELS if key in row},
            "target_step_id": len(row["prompt_dialogue"]),
            "_steps": to_steps(row),
        }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Check ALL rows, including lossless reconstruction and conditional links."""
    issues: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    labels: dict[str, Counter[str]] = {key: Counter() for key in LABELS}
    # Lightweight hashes, not copied dialogues, suffice to verify phase links.
    parents: dict[str, tuple[str, str, str, str, bool]] = {}
    children: list[tuple[str, str, str, str, str, str]] = []
    clusters: Counter[str] = Counter()
    trace_keys: set[str] = set()
    ids: set[str] = set()
    for record_id, row in iter_records(root):
        totals["rows"] += 1
        meta = metadata(record_id)
        phase, target = meta["phase"], meta["target_label"]
        phases[phase] += 1
        clusters[meta["cluster_key"]] += 1
        key = hashed_id(record_id)
        issues["duplicate_record_ids"] += record_id in ids
        issues["trace_key_collisions"] += key in trace_keys
        ids.add(record_id)
        trace_keys.add(key)
        present = [label for label in LABELS if label in row]
        issues["native_label_schema"] += present != [target] or type(row.get(target)) is not bool
        if type(row.get(target)) is bool:
            labels[target]["positive" if row[target] else "negative"] += 1
        dialogue = row["prompt_dialogue"]
        valid = (
            isinstance(dialogue, list)
            and isinstance(row["response"], str)
            and all(
                isinstance(message, dict)
                and set(message) == {"role", "content"}
                and message["role"] in {"system", "user", "assistant"}
                and isinstance(message["content"], str)
                for message in dialogue
            )
        )
        if not valid:
            issues["dialogue_schema"] += 1
            continue
        steps = to_steps(row)
        totals["steps"] += len(steps)
        totals["prompt_messages"] += len(dialogue)
        issues["step_ids"] += [step["step_id"] for step in steps] != list(range(len(steps)))
        restored = [{"role": step["original_role"], "content": step["message"]} for step in steps]
        issues["lossless_roundtrip"] += restored != [
            *dialogue,
            {"role": "assistant", "content": row["response"]},
        ]
        issues["detector_allowlist"] += set(detector_input(row)) != {"prompt_dialogue", "response"}
        issues["step_field_allowlist"] += any(
            set(step) != {"step_id", "source", "original_role", "message"} for step in steps
        )
        issues["source_mapping"] += any(
            step["source"] != ("agent" if step["original_role"] == "assistant" else "user")
            for step in steps
        )
        parents[record_id] = (
            digest(dialogue),
            digest(row["response"]),
            digest(" ".join(row["response"].split())),
            row["model"],
            row[target],
        )
        if phase != "misalignment":
            parent_phase = "misalignment" if phase == "deception" else "deception"
            parent_id = record_id.replace(f"/{phase}/", f"/{parent_phase}/")
            issues["child_dialogue_shape"] += (
                len(dialogue) < 2 or dialogue[-2]["role"] != "assistant"
            )
            children.append(
                (
                    parent_id,
                    phase,
                    digest(dialogue[:-2]),
                    digest(dialogue[-2]["content"]),
                    digest(" ".join(dialogue[-2]["content"].split())),
                    row["model"],
                )
            )
    link_counts: Counter[str] = Counter()
    for parent_id, phase, history, response, normalized, model in children:
        parent = parents.get(parent_id)
        if parent is None:
            issues["missing_phase_parent"] += 1
            continue
        link_counts[f"{phase}_linked"] += 1
        issues["parent_history_mismatch"] += history != parent[0]
        issues["parent_model_mismatch"] += model != parent[3]
        issues["parent_not_positive"] += not parent[4]
        if response != parent[1]:
            link_counts[f"{phase}_parent_response_whitespace_only_difference"] += (
                normalized == parent[2]
            )
            issues["parent_response_nonwhitespace_mismatch"] += normalized != parent[2]
    issues["phase_counts"] += dict(phases) != EXPECTED_PHASE_COUNTS
    issues["root_cluster_count"] += len(clusters) != EXPECTED_PHASE_COUNTS["misalignment"]
    result = {
        "rows": totals["rows"],
        "steps": totals["steps"],
        "prompt_messages": totals["prompt_messages"],
        "phase_counts": dict(sorted(phases.items())),
        "labels": {key: dict(value) for key, value in labels.items()},
        "linked_root_clusters": len(clusters),
        "cluster_sizes": dict(sorted(Counter(clusters.values()).items())),
        "links": dict(sorted(link_counts.items())),
        "dropped_detector_text_characters": 0,
        "issues": {key: value for key, value in sorted(issues.items()) if value},
        "issue_count": sum(issues.values()),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(bool(result["issue_count"]))


if __name__ == "__main__":
    main()
