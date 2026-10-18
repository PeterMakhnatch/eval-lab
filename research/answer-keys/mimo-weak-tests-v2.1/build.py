"""Build the complete MiMo weak-test atlas v2.1 without rewriting v2.

    uv run python research/answer-keys/mimo-weak-tests-v2.1/build.py [--check]

The reviewed atlas is the input. Archived controls are not fresh witness replays.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

KEY_ID = "mimo-weak-tests-v2.1"
HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "mimo-weak-tests-v2"
PREVIOUS_ATLAS_SHA256 = "67c2ab7a0b6ba2849b14daa5fa69c9b322f1f509c28ff1facc087cdb19dc8c4a"
REVIEW_BATCH = "archived-112-20261018"
REVIEW_LABELS = {"required_gap", "non_positive", "uncertain"}


def load_previous_builder():
    spec = importlib.util.spec_from_file_location("mimo_weak_tests_v2_builder", PREVIOUS / "build.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("the committed v2 producer is unavailable")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


def survivors(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    result = {}
    for row in rows:
        for scan in row["line"]["scans"]:
            for mutant in scan["survivors"]:
                key = row["task_id"], scan["source"], mutant["id"]
                if key in result:
                    raise ValueError(f"duplicate archived survivor {key}")
                result[key] = mutant
    return result


def build() -> dict[str, str]:
    builder = load_previous_builder()
    previous_bytes = (PREVIOUS / "atlas.jsonl").read_bytes()
    if hashlib.sha256(previous_bytes).hexdigest() != PREVIOUS_ATLAS_SHA256:
        raise ValueError("the frozen v2 inventory no longer matches its accepted publication")
    previous = [json.loads(line) for line in previous_bytes.decode().splitlines() if line.strip()]
    archived = {
        key: mutant
        for key, mutant in survivors(previous).items()
        if mutant["review"]["label"] == "unreviewed"
    }
    if len(archived) != 112 or len({key[0] for key in archived}) != 22:
        raise ValueError("the authorized archived backlog must remain exactly 112 survivors/22 tasks")
    atlas = builder.jsonl(HERE / "atlas.jsonl")
    if any(row["schema"] != "mimo.weak_test_atlas/v2.1" for row in atlas):
        raise ValueError("the complete inventory must identify the v2.1 schema")
    current = survivors(atlas)
    reviewed = {
        key: mutant
        for key, mutant in current.items()
        if mutant["review"].get("batch") == REVIEW_BATCH
    }
    if reviewed.keys() != archived.keys():
        raise ValueError("the v2.1 review batch must cover every and only the 112 archived survivors")
    for key, mutant in reviewed.items():
        review = mutant["review"]
        if review["label"] not in REVIEW_LABELS or review.get("accepted_by") != "Traces":
            raise ValueError(f"{key}: archived survivor lacks an accepted semantic adjudication")
        original = archived[key]
        for field in original.keys() - {"review", "patch"}:
            if mutant[field] != original[field]:
                raise ValueError(f"{key}: adjudication changed the archived {field}")
        for field in original["patch"].keys() - {"retained_witness"}:
            if mutant["patch"][field] != original["patch"][field]:
                raise ValueError(f"{key}: adjudication changed the original patch binding")
    if any(mutant["review"]["label"] == "unreviewed" for mutant in current.values()):
        raise ValueError("v2.1 must leave no archived line survivor unreviewed")
    if any("archived_line_survivors_unreviewed" in row["unknowns"] for row in atlas):
        raise ValueError("v2.1 retains a stale unreviewed-survivor disposition")
    outputs = builder.build(
        here=HERE,
        key_id=KEY_ID,
        additional_split_keys=("mimo-weak-tests-v2",),
        findings_name="findings-supported-v2.1.jsonl",
    )
    summary = json.loads(outputs["summary.json"])
    summary["previous_key"] = "mimo-weak-tests-v2"
    summary["previous_atlas_sha256"] = PREVIOUS_ATLAS_SHA256
    summary["archived_backlog"] = {
        "tasks": 22,
        "survivors": 112,
        "review_counts": dict(sorted(Counter(m["review"]["label"] for m in reviewed.values()).items())),
        "unreviewed": 0,
        "method": "Archived prompt, complete patch and verifier-source adjudication; no fresh replay.",
    }
    outputs["summary.json"] = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare deterministic outputs")
    args = parser.parse_args()
    outputs = build()
    if args.check:
        stale = [
            name
            for name, text in outputs.items()
            if not (HERE / name).is_file() or (HERE / name).read_text() != text
        ]
        if stale:
            print(f"stale: {stale}", file=sys.stderr)
            return 1
    else:
        for name, text in outputs.items():
            (HERE / name).write_text(text, encoding="utf-8")
    print(outputs["summary.json"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
