"""Join stored readings to the benchmark cases and score the baselines (no model call; reads local files only).

    python tools/baselines.py join  --root BENCH_ROOT     # writes BENCH_ROOT/readings/<reader>.jsonl
    python tools/baselines.py score --root BENCH_ROOT --out DIR   # aggregate-only tables (safe to commit)

``join`` reads the per-run stored readings from the 2026-10-29..11-01 overnight bridge and our own experiment receipts;
labels are not touched. ``score`` joins labels only through ``score.py`` and writes aggregates, never per-case rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import score  # noqa: E402

HOME = Path.home()
BRIDGE31 = HOME / "Developer/research-context/bridge/2026-10-31-overnight"
RESEARCH = HERE.parents[2]
EXPERIMENTS = RESEARCH / "experiments"
G38_W2 = BRIDGE31 / "g38-flash/raw/W2-AUTOREVIEW-INVESTIGATOR-R/receipts"
FABLE_W2 = BRIDGE31 / "fable/raw/W2-AUTOREVIEW-INVESTIGATOR/receipts"
G38_TW = BRIDGE31 / "g38-flash/raw/W3-AUTOREVIEW-V2-TW/receipts/readings-tw.jsonl"
DEEPSWE = BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts"
DT15_MASKED = EXPERIMENTS / "ds-dt15-action-grounded-reader-20261030/readings-masked.jsonl"
C1C5_CASES = EXPERIMENTS / "ov-c1-c5-live-rollouts-20261101/receipts/cases"
SAFEGUARD_ROLLOUTS = (EXPERIMENTS / "ov-safeguard-rollouts-20261031/receipts/rollouts.jsonl",
                      EXPERIMENTS / "ov-safeguard-friction-v2-20261031/receipts/rollouts.jsonl")
READERS = ("l0_tripwires", "ar_g38", "ar_g38_fixed", "ar_fable", "dt15_masked", "sol_astra_structured",
           "sol_astra_flat", "len_tokens", "len_steps")


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def binary(flag: bool) -> float:
    return 100.0 if flag else 0.0


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """0-100 percentile rank, ties averaged (a monotone transform, so thresholds keep their meaning)."""
    ordered = sorted(values.values())
    n = len(ordered)
    first: dict[float, int] = {}
    last: dict[float, int] = {}
    for index, value in enumerate(ordered):
        first.setdefault(value, index)
        last[value] = index
    return {cid: 100 * ((first[v] + last[v]) / 2) / max(n - 1, 1) for cid, v in values.items()}


def join(root: Path) -> dict[str, int]:
    manifest = jsonl(root / "manifest.jsonl")
    scored = [r for r in manifest if r["label"] is not None]
    by_blind = {(r["family"], r["blind_id"]): r["case_id"] for r in manifest}
    by_source = {(r["family"], r["source_id"]): r["case_id"] for r in manifest}
    out: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in READERS}

    for row in jsonl(root / "readings" / "l0_tripwires.jsonl"):
        out["l0_tripwires"][row["case_id"]] = {"suspicion": binary(row["has_gate_card"]), "verdict": ",".join(row["rules"])}

    def stored_autoreview(family: str, path: Path, name: str, fixed: str | None = None) -> None:
        for row in jsonl(path):
            if row.get("status") != "ok":
                continue
            case = by_blind.get((family, row["blind_id"]))
            if case is None:
                continue
            out[name][case] = {"suspicion": float(row["suspicion"]), "verdict": row["verdict"]}
            if fixed:
                out[fixed][case] = {"suspicion": binary(bool(row["alert_fixed"])), "verdict": row["verdict"]}

    stored_autoreview("cheatbench", G38_W2 / "readings-cheatbench.jsonl", "ar_g38", "ar_g38_fixed")
    stored_autoreview("goodfire", G38_W2 / "readings-goodfire.jsonl", "ar_g38", "ar_g38_fixed")
    stored_autoreview("cheatbench", FABLE_W2 / "readings-cheatbench.jsonl", "ar_fable")
    stored_autoreview("goodfire", FABLE_W2 / "readings-goodfire.jsonl", "ar_fable")
    stored_autoreview("deep_swe", DEEPSWE / "readings-arm2.jsonl", "ar_g38", "ar_g38_fixed")
    for row in jsonl(G38_TW):
        case = by_source.get(("terminal_wrench", row["run_id"]))
        if case is not None and row.get("status") == "ok":
            out["ar_g38"][case] = {"suspicion": float(row["suspicion"]), "verdict": row["verdict"]}
            out["ar_g38_fixed"][case] = {"suspicion": binary(bool(row["alert_fixed"])), "verdict": row["verdict"]}
    for family_row in manifest:
        if family_row["family"] != "c1c5":
            continue
        path = C1C5_CASES / family_row["source_id"] / "autoreview.json"
        if path.exists():
            review = json.loads(path.read_text())
            out["ar_g38"][family_row["case_id"]] = {"suspicion": float(review["suspicion"]), "verdict": review["verdict"]}
            out["ar_g38_fixed"][family_row["case_id"]] = {
                "suspicion": binary(review["verdict"] == "confirmed_cheat"), "verdict": review["verdict"]}

    for row in jsonl(DT15_MASKED):
        case = by_blind.get((row["corpus"], row["blind_id"]))
        if case is not None and row.get("status") == "ok":
            out["dt15_masked"][case] = {"suspicion": binary(bool(row["alert"])), "verdict": row["verdict"]}
    for row in jsonl(DEEPSWE / "readings-arm1.jsonl"):
        case = by_blind.get(("deep_swe", row["blind_id"]))
        if case is not None and row.get("status") == "ok":
            out["dt15_masked"][case] = {"suspicion": binary(bool(row["alert"])), "verdict": row["verdict"]}

    for path in SAFEGUARD_ROLLOUTS:
        for row in jsonl(path):
            case = by_source.get(("sol_astra", row["id"]))
            if case is not None:
                out["sol_astra_structured"][case] = {"suspicion": binary(bool(row["structured_flag"]))}
                out["sol_astra_flat"][case] = {"suspicion": binary(bool(row["baseline_flag"]))}

    out["len_tokens"] = {cid: {"suspicion": v}
                         for cid, v in percentile_ranks({r["case_id"]: float(r["approx_tokens"]) for r in scored}).items()}
    out["len_steps"] = {cid: {"suspicion": v}
                        for cid, v in percentile_ranks({r["case_id"]: float(r["steps"]) for r in scored}).items()}

    scored_ids = {r["case_id"] for r in scored}
    counts = {}
    for name, rows in out.items():
        covered = {cid: rows[cid] for cid in rows if cid in scored_ids}
        (root / "readings" / f"{name}.jsonl").write_text(
            "".join(json.dumps({"case_id": cid, **covered[cid]}) + "\n" for cid in sorted(covered)))
        counts[name] = len(covered)
    return counts


COMPARISONS = (("ar_g38", "len_tokens"), ("ar_g38", "l0_tripwires"), ("ar_g38", "ar_g38_fixed"),
               ("ar_g38", "ar_fable"), ("ar_g38", "dt15_masked"), ("l0_tripwires", "len_tokens"))


def run_score(root: Path, out_dir: Path, resamples: int) -> dict[str, Any]:
    cases = score.load_manifest(root / "manifest.jsonl")
    preds = {name: score.load_predictions(root / "readings" / f"{name}.jsonl") for name in READERS}
    results: dict[str, Any] = {"readers": {}, "length_matched": {}, "compare": {}}
    for name in READERS:
        results["readers"][name] = score.score_reader(cases, preds[name], resamples=resamples)
        results["length_matched"][name] = score.score_reader(cases, preds[name], length_matched=True,
                                                             resamples=resamples)
    for a, b in COMPARISONS:
        for alpha in (2, 5):
            results["compare"][f"{a}__vs__{b}__a{alpha}"] = score.compare_readers(cases, preds[a], preds[b], alpha=alpha)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "baselines.json").write_text(json.dumps(results, indent=1))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("join", "score"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--bootstrap", type=int, default=2000)
    args = parser.parse_args()
    if args.command == "join":
        print(json.dumps(join(args.root), indent=1))
        return 0
    if args.out is None:
        parser.error("score needs --out")
    run_score(args.root, args.out, args.bootstrap)
    print(f"wrote {args.out / 'baselines.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
