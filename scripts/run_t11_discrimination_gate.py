"""Run the T1.1 outcome-lineage discrimination gate over featured traj_features rows.

Implements FEATURE-DEBT-LEDGER-2026-08-28.md §7 item T1.1: the discrimination
gate is feedable on the legacy `traj_features` table filtered to
`status = 'featured'` (the ledger's default-denominator rule, §8.4).

Wiring decisions:
- Quarantined features are excluded from the gate inputs via
  `FeatureDefinition.is_quarantined()` (ledger §8.2: excluded from every
  comparison, ranking, and aggregate — kept in the registry).
- The outcome is `task_success = primary_reward == 1.0`, matching the repo
  convention (`evallab.queue` derives the same boolean).
- The outcome column itself is not fed back as a predictor feature.
- Refusals (UNDERPOWERED, SINGLE_OUTCOME_CLASS, structural violations) are
  informative results, not failures.

Read-only over the parquet; writes a JSON artifact under research/analysis/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb

from evallab.analysis_capability import (
    FeatureObservation,
    evaluate_process_outcome_gate,
)
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    feature_contract_row,
)

DEFAULT_PARQUET = Path("derived/parquet/traj_features/traj_features.parquet")
DEFAULT_OUTPUT = Path("research/analysis/t11_discrimination_gate.json")
OUTCOME_COLUMN = "primary_reward"
STATUS_COLUMN = "status"
FEATURED_STATUS = "featured"

_VALUE_COERCIONS: dict[str, Any] = {
    "BIGINT": int,
    "DOUBLE": float,
    "BOOLEAN": bool,
    "VARCHAR": str,
}


def _coerce_value(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    coerced = _VALUE_COERCIONS[data_type](value)
    if isinstance(coerced, float) and math.isnan(coerced):
        return None
    return coerced


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clearance-n", type=int, default=20)
    args = parser.parse_args()

    parquet_path = args.parquet
    if not parquet_path.exists():
        raise SystemExit(f"parquet not found: {parquet_path}")
    snapshot_digest = _file_digest(parquet_path)

    registry = TRAJECTORY_FEATURE_REGISTRY.all_features()

    # Ledger §8.2: quarantined features stay registered but are excluded from
    # every comparison. The exclusion is wired through FeatureDefinition.is_quarantined().
    quarantined = {name: feat for name, feat in registry.items() if feat.is_quarantined()}
    candidates = {
        name: feat for name, feat in registry.items() if not feat.is_quarantined()
    }

    con = duckdb.connect()
    quoted = str(parquet_path).replace("'", "''")
    rows = con.execute(
        f"SELECT * FROM read_parquet('{quoted}') "
        f"WHERE {STATUS_COLUMN} = '{FEATURED_STATUS}'"
    ).fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()
    records = [dict(zip(columns, row, strict=True)) for row in rows]
    n_featured_rows = len(records)
    # A trial_id can be double-ingested from two source roots; the paired rows
    # differ only in source_path/source_sha256 (verified 2026-09-02: every
    # feature column and primary_reward identical). Keep one row per trial_id,
    # deterministically the lowest source_sha256, so the gate sees unique units.
    by_trial: dict[str, dict[str, Any]] = {}
    for record in records:
        trial_id = str(record["trial_id"])
        digest_key = str(record["source_sha256"])
        if trial_id not in by_trial or digest_key < str(by_trial[trial_id]["source_sha256"]):
            by_trial[trial_id] = record
    records = [by_trial[tid] for tid in sorted(by_trial)]

    n_success = sum(1 for r in records if r[OUTCOME_COLUMN] == 1.0)
    n_failure = len(records) - n_success

    # Contracts: every registered, non-quarantined feature is audited — including
    # registered-but-unproduced features, whose absence is itself ledger debt (§5).
    contracts = [feature_contract_row(feat) for feat in candidates.values()]

    # Observations: only features whose column exists in this snapshot.
    # The outcome column is the gate's target, never a predictor row.
    observed_features = sorted(
        name for name in candidates if name in columns and name != OUTCOME_COLUMN
    )
    observations: list[FeatureObservation] = []
    for record in records:
        trial_id = str(record["trial_id"])
        task_success = record[OUTCOME_COLUMN] == 1.0
        for name in observed_features:
            feat = candidates[name]
            observations.append(
                FeatureObservation(
                    feature_name=name,
                    trial_id=trial_id,
                    task_success=task_success,
                    value=_coerce_value(record[name], feat.data_type),
                )
            )

    report = evaluate_process_outcome_gate(
        contracts,
        observations,
        source_analysis_snapshot_digest=snapshot_digest,  # type: ignore[arg-type]
        clearance_n=args.clearance_n,
    )

    verdict_counts = Counter(result.verdict.value for result in report.results)
    refusal_counts = Counter(
        str(result.empirical.refusal_code.value)
        for result in report.results
        if result.empirical.refusal_code is not None
    )
    data_absent = sorted(set(candidates) - set(observed_features) - {OUTCOME_COLUMN})

    print(f"snapshot: {parquet_path}")
    print(f"snapshot_digest: {snapshot_digest}")
    print(f"featured rows: {n_featured_rows} -> {len(records)} unique trials "
          f"(task_success={n_success}, task_failure={n_failure})")
    print(f"registry features: {len(registry)} (quarantined excluded: {len(quarantined)})")
    print(f"gate contracts: {len(contracts)} (observed columns: {len(observed_features)}, "
          f"data-absent: {len(data_absent)})")
    print(f"observations: {len(observations)}")
    print(f"clearance_n: {args.clearance_n}")
    print()
    print("verdicts:")
    for verdict, count in sorted(verdict_counts.items()):
        print(f"  {verdict}: {count}")
    print("empirical refusal codes:")
    for code, count in sorted(refusal_counts.items()):
        print(f"  {code}: {count}")
    print()
    print("quarantined features excluded from inputs:")
    by_reason = Counter(feat.quarantine_reason for feat in quarantined.values())
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}")
    print()
    print("per-feature results (observed columns):")
    by_name = {result.feature_name: result for result in report.results}
    for name in observed_features:
        result = by_name[name]
        empirical = result.empirical
        auc = f"{empirical.auc_x_to_task_success:.3f}" if empirical.auc_x_to_task_success is not None else "-"
        print(
            f"  {name}: verdict={result.verdict.value} n_nonnull={empirical.n_nonnull} "
            f"auc={auc} zero_var={empirical.zero_variance} "
            f"refusal={empirical.refusal_code.value if empirical.refusal_code else '-'}"
        )

    artifact = {
        "source_parquet": str(parquet_path),
        "source_snapshot_digest": snapshot_digest,
        "featured_rows_raw": n_featured_rows,
        "featured_rows_unique_trials": len(records),
        "task_success_rows": n_success,
        "task_failure_rows": n_failure,
        "clearance_n": args.clearance_n,
        "quarantined_excluded": {
            name: feat.quarantine_reason for name, feat in sorted(quarantined.items())
        },
        "observed_features": observed_features,
        "data_absent_features": data_absent,
        "unregistered_parquet_columns": sorted(set(columns) - set(registry)),
        "t11_report": report.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    print()
    print(f"report_digest: {report.report_digest}")
    print(f"input_digest: {report.input_digest}")
    print(f"artifact: {args.output}")


if __name__ == "__main__":
    main()
