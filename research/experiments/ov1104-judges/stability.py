"""Agreement between a judge batch and its re-run (different temperature/seed) on the same runs.

Usage::

    uv run python research/experiments/ov1104-judges/stability.py --a <batch dir> --b <rerun batch dir> \\
        --threshold <5%-FPR threshold of the frozen judge>

Reports n paired, Spearman rank correlation of scores, mean absolute difference, and agreement of
the flag (score >= threshold) with a Wilson 95% interval.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = rank
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else float("nan")


def load(batch: Path) -> dict[str, float | None]:
    out = {}
    for path in (batch / "results").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        out[row["run_key"]] = row.get("score")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", type=Path, required=True)
    parser.add_argument("--b", type=Path, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    a, b = load(args.a), load(args.b)
    keys = sorted(k for k in b if k in a)
    paired = [(a[k], b[k]) for k in keys if a[k] is not None and b[k] is not None]
    abstain_disagree = sum((a[k] is None) != (b[k] is None) for k in keys)
    xs, ys = [p[0] for p in paired], [p[1] for p in paired]
    agree = sum((x >= args.threshold) == (y >= args.threshold) for x, y in paired)
    result = {
        "n_rerun": len(keys), "n_paired": len(paired), "abstain_disagreements": abstain_disagree,
        "spearman": spearman(xs, ys) if len(paired) > 2 else None,
        "mean_abs_diff": sum(abs(x - y) for x, y in paired) / len(paired) if paired else None,
        "threshold": args.threshold, "flag_agreement": agree / len(paired) if paired else None,
        "flag_agreement_wilson95": wilson(agree, len(paired)),
        "flags_a": sum(x >= args.threshold for x in xs), "flags_b": sum(y >= args.threshold for y in ys),
    }
    print(json.dumps(result, indent=1))
    if args.out:
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
