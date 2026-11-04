"""Judge results -> DetBench signal files, plus cross-family ensembles.

Usage::

    uv run python research/experiments/ov1104-judges/signals.py --out-dir /private/tmp/ov1104/judges/signals \\
        --batch j1_flash=/private/tmp/ov1104/judges/out/j1__google-antigravity__gemini-3.8-flash \\
        --batch j3_fable=/private/tmp/ov1104/judges/out/j2__anthropic__claude-fable-5-1 \\
        --ensemble j_mean=j1_flash+j3_fable:mean --ensemble j_max=j1_flash+j3_fable:max

Signal rows: ``{"run_key", "signal", "version", "score", "abstain", "evidence_steps", "model",
"cost_usd"}``. ``version`` is the sha256 of the judge code + rubric + model id (threshold is
appended by FREEZE.md for the frozen judge). Every subscription call costs $0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CODE_FILES = ("judge.py", "views.py", "prompts.py", "gateway.py", "j0_autoreview.py")


def code_sha() -> str:
    digest = hashlib.sha256()
    for name in CODE_FILES:
        digest.update((HERE / name).read_bytes())
    return digest.hexdigest()


def load_batch(batch_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((batch_dir / "results").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        out[row["run_key"]] = row
    return out


def version_of(results: dict[str, dict[str, Any]], model: str) -> str:
    rubric = next((r.get("rubric_sha256") for r in results.values() if r.get("rubric_sha256")), "deployed-autoreview")
    return hashlib.sha256(f"{code_sha()}|{rubric}|{model}".encode()).hexdigest()


def signal_rows(name: str, results: dict[str, dict[str, Any]], model: str) -> list[dict[str, Any]]:
    version = version_of(results, model)
    rows = []
    for run_key, row in sorted(results.items()):
        served = sorted({w.get("model_served") for w in row.get("windows", []) if w.get("model_served")})
        model_served = row.get("cached_model") if row.get("source") == "cached-autoreview" else (served[0] if len(served) == 1 else (",".join(served) or None))
        rows.append({
            "run_key": run_key, "signal": name, "version": version,
            "score": row.get("score"), "abstain": row.get("abstain") if row.get("score") is None else None,
            "evidence_steps": [int(s) for s in row.get("evidence_steps") or []][:5],
            "model": model_served, "cost_usd": 0.0,
        })
    return rows


def ensemble_rows(name: str, parts: list[list[dict[str, Any]]], method: str) -> list[dict[str, Any]]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for rows in parts:
        for row in rows:
            by_key.setdefault(row["run_key"], []).append(row)
    version = hashlib.sha256((method + "|" + "|".join(sorted({r["version"] for rows in parts for r in rows}))).encode()).hexdigest()
    out = []
    for run_key, rows in sorted(by_key.items()):
        scores = [r["score"] for r in rows if r["score"] is not None]
        if not scores:
            out.append({"run_key": run_key, "signal": name, "version": version, "score": None,
                        "abstain": "; ".join(sorted({r["abstain"] or "missing" for r in rows})), "evidence_steps": [], "model": None, "cost_usd": 0.0})
            continue
        score = max(scores) if method == "max" else sum(scores) / len(scores)
        top = max((r for r in rows if r["score"] is not None), key=lambda r: r["score"])
        out.append({"run_key": run_key, "signal": name, "version": version, "score": score,
                    "abstain": None if len(scores) == len(rows) else f"partial: {len(scores)}/{len(rows)} judges scored",
                    "evidence_steps": top["evidence_steps"], "model": ",".join(sorted({str(r["model"]) for r in rows})), "cost_usd": 0.0})
    return out


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--batch", action="append", default=[], help="name=batch_dir")
    parser.add_argument("--ensemble", action="append", default=[], help="name=a+b:mean|max")
    args = parser.parse_args()
    signals: dict[str, list[dict[str, Any]]] = {}
    for spec in args.batch:
        name, batch_dir = spec.split("=", 1)
        results = load_batch(Path(batch_dir))
        model = next(iter(results.values()))["model"] if results else "unknown"
        signals[name] = signal_rows(name, results, model)
    for spec in args.ensemble:
        name, rest = spec.split("=", 1)
        members, method = rest.split(":")
        signals[name] = ensemble_rows(name, [signals[m] for m in members.split("+")], method)
    for name, rows in signals.items():
        write(args.out_dir / f"{name}.jsonl", rows)
        scored = sum(r["score"] is not None for r in rows)
        print(f"{name}: {len(rows)} rows, {scored} scored, {len(rows) - scored} abstain, version {rows[0]['version'][:12] if rows else '-'}")


if __name__ == "__main__":
    main()
