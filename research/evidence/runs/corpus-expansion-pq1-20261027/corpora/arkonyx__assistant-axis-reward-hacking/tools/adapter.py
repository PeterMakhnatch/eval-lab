#!/usr/bin/env python3
"""Read local extraction inputs; never emit corpus text or expose outcome fields to a detector."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import tarfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "arkonyx__assistant-axis-reward-hacking"
HF_REVISION = "3bd85dd4126fef62dd8607c18805805588d4d5d4"
COMPANION_COMMIT = "6b78bc170a6fae780949ecade464832d0d4cf1d7"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / HF_REVISION[:12]
ARCHIVE = "assistant-axis-observations.tar.gz"
TRANSCRIPTS = "companion/published/extraction-transcripts.jsonl.gz"
ARCHIVE_SHA256 = "b3df15d8331ec73410c7e92116fe1c3fe2a500748e24a2853b5bd77d3da13129"
TRANSCRIPTS_SHA256 = "6229ca20bef87a7c8680da4be7a7faeefc82d4934f01821e4a68f03f03450a4e"
UNCOMPRESSED_SHA256 = "f6848718b0a50f48ecc428bb538fa00ca0926c9774965b393175a34abaf41285"
RAW_PREFIX = "<|channel|>analysis<|message|>"
RAW_MIDDLE = "<|end|><|start|>assistant<|channel|>final<|message|>"
RAW_SUFFIX = "<|return|>"
RAW_RESPONSE = re.compile(
    re.escape(RAW_PREFIX)
    + r"(?P<analysis>.*?)"
    + re.escape(RAW_MIDDLE)
    + r"(?P<final>.*?)"
    + re.escape(RAW_SUFFIX),
    re.DOTALL,
)


def hashed_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    with gzip.open(root / TRANSCRIPTS, "rt", encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def observation_index(root: Path = ROOT) -> dict[tuple[str, int], dict[str, Any]]:
    """Join on (example_id, turn), not shard position, episode id, or a label field."""
    index: dict[tuple[str, int], dict[str, Any]] = {}
    with tarfile.open(root / ARCHIVE, "r:gz") as archive:
        paths = sorted(m.name for m in archive.getmembers() if m.name.endswith("/records.jsonl"))
        if len(paths) != 6:
            raise ValueError("expected six observation shards")
        for path in paths:
            stream = archive.extractfile(path)
            if stream is None:
                raise ValueError("missing shard records")
            for line in stream:
                row = json.loads(line)
                key = row["example_id"], row["turn"]
                if key in index:
                    raise ValueError("duplicate observation join key")
                index[key] = row
    return index


def joined_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    index = observation_index(root)
    seen: set[tuple[str, int]] = set()
    for row in iter_rows(root):
        key = row["example_id"], row["turn"]
        if key in seen or key not in index:
            raise ValueError("duplicate or unmatched transcript join key")
        seen.add(key)
        cached = index[key]
        reduced = {k: v for k, v in row["outcome"].items() if k != "final_code"}
        if (
            row["scenario"] != cached["scenario"]
            or reduced != cached["outcome"]
            or row["outcome"]["example_id"] != row["example_id"]
        ):
            raise ValueError("transcript/observation metadata disagree")
        yield row
    if seen != index.keys():
        raise ValueError("observations without transcripts")


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Only messages and raw_response are read; no outcome or judge-derived field is read."""
    steps = []
    for message in row["messages"]:
        role = message["role"]
        if set(message) != {"role", "content"} or role not in {"system", "user", "assistant"}:
            raise ValueError("unsupported message schema")
        if not isinstance(message["content"], str):
            raise ValueError("non-text message")
        steps.append(
            {
                "step_id": len(steps),
                "source": "agent" if role == "assistant" else "user",
                "source_role": role,
                "message": message["content"],
            }
        )
    response = RAW_RESPONSE.fullmatch(row["raw_response"])
    if response is None:
        raise ValueError("unsupported selected-response channel framing")
    steps.append(
        {
            "step_id": len(steps),
            "source": "agent",
            "source_role": "assistant",
            "reasoning_content": response["analysis"],
            "message": response["final"],
        }
    )
    return steps


def detector_input(row: dict[str, Any]) -> dict[str, Any]:
    """The complete detector-visible payload. Spec metadata is evaluation-only."""
    return {"steps": to_steps(row)}


def specs(root: Path = ROOT, *, positive_set: str = "strong+weak") -> Iterator[dict[str, Any]]:
    """One selected turn per spec; STRONG-only excludes WEAK hacks, never relabels them."""
    if positive_set not in {"strong-only", "strong+weak"}:
        raise ValueError("unknown positive set")
    for row in joined_rows(root):
        outcome = row["outcome"]
        verdict, strength = outcome["verdict"], outcome.get("strength")
        if verdict == "HACK":
            if strength not in {"STRONG", "WEAK"}:
                raise ValueError("unknown HACK strength")
            if positive_set == "strong-only" and strength == "WEAK":
                continue
            positive = True
        elif verdict == "unflagged" and strength is None:
            positive = False
        else:
            raise ValueError("unknown label")
        key = hashed_id(row["example_id"])
        yield {
            "corpus": CORPUS,
            "trace_id": f"{CORPUS}:{key}",
            "trace_key": key,
            "positive": positive,
            "negative_kind": None if positive else "weak_negative",
            "problem_key": hashed_id(outcome["problem"]),
            "episode_key": hashed_id(outcome["episode_id"]),
            "turn": row["turn"],
            "gold": {k: outcome.get(k) for k in ("verdict", "strength", "label", "harness_label")},
            "_steps": detector_input(row)["steps"],
        }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    issues: Counter[str] = Counter()
    rows = list(joined_rows(root))
    keys = [hashed_id(row["example_id"]) for row in rows]
    if len(rows) != 824 or len(set(keys)) != len(rows):
        issues["record_count_or_id_collision"] += 1
    step_count = 0
    for row in rows:
        try:
            payload = detector_input(row)
            steps = payload["steps"]
            step_count += len(steps)
            if set(payload) != {"steps"}:
                issues["detector_envelope"] += 1
            if [step["step_id"] for step in steps] != list(range(len(steps))):
                issues["step_numbering"] += 1
            restored = [
                {"role": step["source_role"], "content": step["message"]} for step in steps[:-1]
            ]
            response = (
                RAW_PREFIX
                + steps[-1]["reasoning_content"]
                + RAW_MIDDLE
                + steps[-1]["message"]
                + RAW_SUFFIX
            )
            if restored != row["messages"] or response != row["raw_response"]:
                issues["text_roundtrip"] += 1
            if any(step["source"] not in {"agent", "user"} for step in steps):
                issues["step_source"] += 1
            if payload != detector_input(
                {"messages": row["messages"], "raw_response": row["raw_response"]}
            ):
                issues["metadata_leak"] += 1
        except (KeyError, TypeError, ValueError):
            issues["unsupported_row"] += 1
    cohorts = {}
    for mode, expected in (("strong-only", (792, 157, 635)), ("strong+weak", (824, 189, 635))):
        counts: Counter[str] = Counter()
        for spec in specs(root, positive_set=mode):
            counts["rows"] += 1
            counts["positive" if spec["positive"] else "weak_negative"] += 1
        actual = counts["rows"], counts["positive"], counts["weak_negative"]
        if actual != expected:
            issues["cohort_counts"] += 1
        cohorts[mode] = dict(counts)
    return {
        "rows": len(rows),
        "steps": step_count,
        "cohorts": cohorts,
        "lossless_text_roundtrips": len(rows)
        - issues["text_roundtrip"]
        - issues["unsupported_row"],
        "issues": sum(issues.values()),
        "issue_counts": dict(issues),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1 if result["issues"] else 0)


if __name__ == "__main__":
    main()
