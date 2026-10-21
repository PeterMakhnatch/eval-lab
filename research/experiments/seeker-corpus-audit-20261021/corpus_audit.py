#!/usr/bin/env python3
"""Data-quality audit of `seeker_tool_sft.jsonl` (pE-SELF-8).

Measures what the corpus can actually teach: how many *distinct* examples it
contains once duplicated `submission/solve.sh` bodies are collapsed, how much of
the assistant content is execution code rather than description, how diverse the
exploits are within a hack class, and which classes are covered.

Free and local: reads the committed corpus, no model and no sandbox.

Usage:
    uv run python research/experiments/seeker-corpus-audit-20261021/corpus_audit.py
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CORPUS = REPO / "research/datasets/seeker-sft-v1/seeker_tool_sft.jsonl"


def _rows() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _solve_sh(row: dict[str, Any]) -> str | None:
    for message in row["messages"]:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "write_file":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                continue
            if str(args.get("path") or "").endswith("submission/solve.sh"):
                return str(args.get("content") or "")
    return None


def _execution_share(rows: list[dict[str, Any]]) -> dict[str, int]:
    description = 0
    execution = 0
    for row in rows:
        for message in row["messages"]:
            if message["role"] != "assistant":
                continue
            description += len(str(message.get("content") or ""))
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") == "write_file":
                    execution += len(str(function.get("arguments") or ""))
    return {"description_chars": description, "execution_chars": execution}


def _diversity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_class: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if not row["exploit_found"]:
            continue
        body = _solve_sh(row)
        if body is not None:
            by_class[row["mechanism_task_class"]].append(body)
    out: dict[str, Any] = {}
    for name, bodies in sorted(by_class.items()):
        similarities = [
            difflib.SequenceMatcher(None, bodies[i], bodies[j]).ratio()
            for i in range(len(bodies))
            for j in range(i + 1, len(bodies))
        ]
        out[name] = {
            "bodies": len(bodies),
            "distinct_bodies": len({hashlib.sha256(b.encode()).hexdigest() for b in bodies}),
            "mean_pairwise_similarity": (
                round(sum(similarities) / len(similarities), 4) if similarities else None
            ),
            "max_pairwise_similarity": round(max(similarities), 4) if similarities else None,
        }
    return out


def main() -> int:
    rows = _rows()
    bodies = [(row, _solve_sh(row)) for row in rows]
    digests = Counter(
        hashlib.sha256(body.encode()).hexdigest()[:12] for _, body in bodies if body is not None
    )
    pairs = {
        (row["task_id"], hashlib.sha256(body.encode()).hexdigest()[:12])
        for row, body in bodies
        if body is not None
    }
    positive_pairs = {
        (row["task_id"], hashlib.sha256(body.encode()).hexdigest()[:12])
        for row, body in bodies
        if body is not None and row["exploit_found"]
    }
    negative_pairs = {
        (row["task_id"], hashlib.sha256(body.encode()).hexdigest()[:12])
        for row, body in bodies
        if body is not None and not row["exploit_found"]
    }
    share = _execution_share(rows)
    total_assistant = share["description_chars"] + share["execution_chars"]

    report = {
        "corpus": CORPUS.as_posix(),
        "conversations": len(rows),
        "exploit_found": dict(Counter(row["exploit_found"] for row in rows)),
        "conditioning_mode": dict(Counter(row["conditioning_mode"] for row in rows)),
        "source_family": dict(Counter(row["source_family"] for row in rows)),
        "mechanism_task_class": dict(Counter(row["mechanism_task_class"] for row in rows)),
        "verified_differential": dict(Counter(row["verified_differential"] for row in rows)),
        "effective_size": {
            "conversations": len(rows),
            "distinct_task_body_pairs": len(pairs),
            "distinct_tasks": len({task for task, _ in pairs}),
            "positive_conversations": sum(1 for row in rows if row["exploit_found"]),
            "positive_distinct_task_body_pairs": len(positive_pairs),
            "negative_conversations": sum(1 for row in rows if not row["exploit_found"]),
            "negative_distinct_task_body_pairs": len(negative_pairs),
            "distinct_bodies": len(digests),
            "duplicated_bodies": sum(1 for count in digests.values() if count > 1),
            "extra_copies": sum(count - 1 for count in digests.values() if count > 1),
        },
        "execution_share": {
            **share,
            "execution_share_of_assistant_content": (
                round(share["execution_chars"] / total_assistant, 4) if total_assistant else None
            ),
        },
        "diversity_by_class": _diversity(rows),
        "top_duplicated_bodies": [
            {"digest": digest, "copies": count}
            for digest, count in digests.most_common(8)
            if count > 1
        ],
    }
    out = HERE / "corpus-audit.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "top_duplicated_bodies"},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
