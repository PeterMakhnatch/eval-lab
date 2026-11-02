"""Generate the SYNTHETIC toy case set under ``toy/`` (no real trace, label or text; deterministic).

Two invented families with the same manifest schema as the real benchmark, so ``score.py`` can be exercised in CI and by
a new reader author without any local third-party data. ``python tools/make_toy.py`` rewrites ``toy/``; a test checks
that the committed files equal this output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_bench  # noqa: E402

TOY = HERE.parent / "toy"
FAMILIES = (("toy_swe", 12, 12), ("toy_chat", 6, 6))


def manifest_rows() -> list[dict]:
    rows = []
    for family, n_pos, n_neg in FAMILIES:
        for label, count, base in ((True, n_pos, 1200), (False, n_neg, 1000)):
            for index in range(count):
                cid = f"toy-{family}-{'p' if label else 'n'}{index:02d}"
                tokens = base + 37 * index
                rows.append({"case_id": cid, "family": family, "label": label, "label_status": "scored",
                             "provenance": "synthetic", "steps": 10 + index, "tool_calls": 5 + index,
                             "raw_chars": 4 * tokens, "approx_tokens": tokens})
    rows.append({"case_id": "toy-toy_chat-x00", "family": "toy_chat", "label": None,
                 "label_status": "excluded_toy", "provenance": "synthetic", "steps": 7, "tool_calls": 3,
                 "raw_chars": 4000, "approx_tokens": 1000})
    build_bench.split_cells(rows)
    return rows


def predictions() -> dict[str, list[dict]]:
    """``graded``: overlapping scores (AUROC below 1). ``binary``: alerts as 100/0, like a verdict-only reader."""
    graded, binary = [], []
    for row in manifest_rows():
        if row["label"] is None:
            continue
        index = int(row["case_id"][-2:])
        if row["label"]:
            score = 40 + (index * 13) % 55
        else:
            score = (index * 17) % 50 + (30 if index % 5 == 0 else 0)
        graded.append({"case_id": row["case_id"], "suspicion": score})
        binary.append({"case_id": row["case_id"], "suspicion": 100 if score >= 60 else 0, "verdict": "x"})
    return {"pred_graded.jsonl": graded, "pred_binary.jsonl": binary}


def render() -> dict[str, str]:
    files = {"manifest.jsonl": "".join(json.dumps(r) + "\n" for r in manifest_rows())}
    for name, rows in predictions().items():
        files[name] = "".join(json.dumps(r) + "\n" for r in rows)
    return files


def main() -> int:
    TOY.mkdir(exist_ok=True)
    for name, text in render().items():
        (TOY / name).write_text(text)
    print(f"wrote {len(render())} files to {TOY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
