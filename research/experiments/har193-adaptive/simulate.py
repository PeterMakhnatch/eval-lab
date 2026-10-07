#!/usr/bin/env python3
"""HAR-193 paired, deterministic offline replay; no models or provider calls.

The adjacent results.json retains the compact native HAR-168 fixture. This
script replaces its analytical reference table with observed simulation rows,
using the production adaptive_band implementation, and retains that fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from evallab import campaign_approval as cap

HERE = Path(__file__).resolve().parent
SEED = 20261007
PROBABILITIES = (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)
TASK_IDS = (
    "000552",
    "000792",
    "001109",
    "001198",
    "001985",
    "002139",
    "002391",
    "002402",
    "002486",
    "002552",
    "002864",
    "002938",
)
COLUMNS = (
    "corpus",
    "p",
    "max_attempts",
    "target_confidence",
    "sequences",
    "fixed_attempts",
    "adaptive_attempts",
    "attempts_saved",
    "mean_saved",
    "early_stops",
    "classification_mismatches",
    "empirical_error_rate",
    "posterior_predictive_mean_error",
    "posterior_predictive_max_error",
    "unclassified",
    "truncated",
    "analytic_mean_saved_at_095",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gated_pass(row: dict) -> bool:
    """Interpret the native gate, without rewriting raw NULL rewards."""
    values = (row["reward"], row["integrity"], row["reward_gated"])
    if row["outcome"] == "context_exhausted":
        if values != (None, None, None):
            raise ValueError("context-exhausted native rewards must remain NULL")
        return False
    if row["outcome"] != "graded" or any(value not in (0, 1) for value in values):
        raise ValueError("fixture must contain binary graded or scoped context outcomes")
    reward, integrity, reward_gated = values
    if reward_gated != reward * integrity:
        raise ValueError("native reward_gated disagrees with reward x integrity")
    return reward_gated == 1 and integrity == 1


def load_sequences(fixture: dict, source_root: Path | None) -> list[list[bool]]:
    rows = fixture["outcomes"]
    if len(rows) != 24:
        raise ValueError("HAR-168 fixture requires exactly 24 selected outcomes")
    grouped: dict[str, dict[int, bool]] = defaultdict(dict)
    for row in rows:
        task_id, attempt = row["task_id"], row["attempt"]
        if task_id not in TASK_IDS or attempt not in (1, 2) or attempt in grouped[task_id]:
            raise ValueError("unexpected or duplicate HAR-168 task/attempt")
        grouped[task_id][attempt] = gated_pass(row)
        if source_root is not None:
            path = source_root / row["result_path"]
            if sha256(path) != row["result_sha256"]:
                raise ValueError(f"native source digest changed: {row['result_path']}")
            native = json.loads(path.read_text(encoding="utf-8"))
            if native["id"] != row["native_trial_id"]:
                raise ValueError("native trial identity changed")
            if native["task_name"] != f"mimo-v2.6-rl/format-code-task-{task_id}":
                raise ValueError("native task identity changed")
            rewards = (native.get("verifier_result") or {}).get("rewards") or {}
            for key in ("reward", "integrity", "reward_gated"):
                if rewards.get(key) != row[key]:
                    raise ValueError(f"native {key} differs from fixture")
            if row["outcome"] == "context_exhausted" and native["verifier_result"] is not None:
                raise ValueError("context-exhausted trial unexpectedly has verifier grades")
    if set(grouped) != set(TASK_IDS) or any(set(values) != {1, 2} for values in grouped.values()):
        raise ValueError("HAR-168 fixture is not the full selected two-attempt cohort")
    if source_root is not None:
        references = fixture["authority_sources"] + fixture["copy_sources"]
        references += [
            {"path": row["result_path"], "sha256": row["result_sha256"]}
            for row in fixture["excluded_infrastructure"]
        ]
        for ref in references:
            if sha256(source_root / ref["path"]) != ref["sha256"]:
                raise ValueError(f"fixture provenance digest changed: {ref['path']}")
    return [[grouped[task_id][1], grouped[task_id][2]] for task_id in TASK_IDS]


def empirical_band(outcomes: list[bool]) -> str:
    """Reference is the complete finite sequence, not a threshold on latent p."""
    if all(outcomes):
        return "always"
    if not any(outcomes):
        return "never"
    return "sometimes"


def summarize(
    corpus: str,
    p: float | None,
    sequences: list[list[bool]],
    max_attempts: int,
    target_confidence: float,
) -> list:
    fixed_attempts = adaptive_attempts = early_stops = mismatches = 0
    classified = unclassified = truncated = 0
    risk_sum = risk_max = 0.0
    for complete in sequences:
        if len(complete) != max_attempts:
            raise ValueError("paired reference must have the complete M-outcome sequence")
        fixed_attempts += max_attempts
        band, confidence = cap.adaptive_band([], max_attempts, target_confidence)
        used = 0
        for used in range(1, max_attempts + 1):
            band, confidence = cap.adaptive_band(complete[:used], max_attempts, target_confidence)
            if band is not None:
                break
        adaptive_attempts += used
        if band is None:
            unclassified += 1
            truncated += used < max_attempts
            continue
        classified += 1
        early_stops += used < max_attempts
        mismatches += band != empirical_band(complete)
        risk = 1.0 - confidence
        risk_sum += risk
        risk_max = max(risk_max, risk)
    saved = fixed_attempts - adaptive_attempts
    analytic_saved = None if p is None else (0.0 if max_attempts == 2 else 5.0 * p * (1.0 - p))
    return [
        corpus,
        p,
        max_attempts,
        target_confidence,
        len(sequences),
        fixed_attempts,
        adaptive_attempts,
        saved,
        saved / len(sequences),
        early_stops,
        mismatches,
        mismatches / classified if classified else None,
        risk_sum / classified if classified else None,
        risk_max if classified else None,
        unclassified,
        truncated,
        analytic_saved,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=HERE / "results.json")
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="optional read-only har164 checkout; verify retained native hashes",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--synthetic-tasks", type=int, default=10000)
    parser.add_argument(
        "--confidence",
        type=float,
        action="append",
        help="repeatable; default .95; .6 is an explicit exploratory tradeoff",
    )
    args = parser.parse_args()
    if args.synthetic_tasks < 1:
        parser.error("--synthetic-tasks must be positive")
    confidences = args.confidence if args.confidence is not None else [0.95]
    if any(not 0.5 < value <= 1.0 for value in confidences):
        parser.error("each --confidence must be in (0.5, 1]")
    confidences = list(dict.fromkeys(confidences))
    document = json.loads(args.fixture.read_text(encoding="utf-8"))
    fixture = document["har168_fixture"]
    observed = load_sequences(fixture, args.source_root)
    table = [
        summarize("har168_observed", None, observed, 2, confidence) for confidence in confidences
    ]
    synthetic_sequence_digests = {}
    for p in PROBABILITIES:
        # One seeded four-draw sequence per task, reused by both horizons and
        # every confidence. A threshold-independent seed also pairs p settings.
        rng = random.Random(args.seed)
        complete4 = [[rng.random() < p for _ in range(4)] for _ in range(args.synthetic_tasks)]
        synthetic_sequence_digests[str(p)] = hashlib.sha256(
            bytes(outcome for sequence in complete4 for outcome in sequence)
        ).hexdigest()
        for max_attempts in (2, 4):
            sequences = complete4 if max_attempts == 4 else [row[:2] for row in complete4]
            for confidence in confidences:
                table.append(
                    summarize("synthetic_bernoulli", p, sequences, max_attempts, confidence)
                )
    output = {
        "schema": "har193.adaptive_simulation/v1",
        "artifact_kind": "executed_offline_simulation",
        "external_spend_usd": 0,
        "seed": args.seed,
        "synthetic_tasks_per_p": args.synthetic_tasks,
        "confidence_targets": confidences,
        "band_definition": {
            "always": "all M gated passes",
            "never": "zero M gated passes",
            "sometimes": "mixed M gated outcomes",
        },
        "reference": "same complete M-outcome sequence; no latent-p band thresholds",
        "source_check": "native hashes and values verified"
        if args.source_root
        else "portable source-bound fixture only",
        "simulation_sha256": sha256(Path(__file__)),
        "production_policy_sha256": sha256(Path(cap.__file__)),
        "fixture_sha256": hashlib.sha256(
            json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "synthetic_sequence_sha256": synthetic_sequence_digests,
        "table": {"columns": COLUMNS, "rows": table},
        "har168_fixture": fixture,
        "limitations": [
            "Offline paired replay, not new model-capability evidence or paid-run authorization.",
            "Posterior predictive error uses Beta(1,1); empirical mismatch uses completed finite-M sequences and need not equal that model risk.",
            "At .95 no homogeneous M2/M4 prefix stops early; mixed M4 prefixes stop conclusively. Analytical saves: M2=0, M4=5p(1-p).",
            "At .6 an M2 first outcome can stop with confidence 2/3, model error 1/3 (<=.4); frequentist mismatch may exceed .4 (p=.5 gives .5).",
            "HAR168 has only two observed attempts; attempts 3/4 are not fabricated. Infra draws are excluded and their authentic replacement retained.",
        ],
    }
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(table)} paired comparison rows to {args.output}; external spend $0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
