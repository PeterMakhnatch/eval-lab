# Envelope-aware outline errors: blast radius — 2026-09-25

> Agent-reviewed. Fix: `traj.py` unwraps mini-swe-agent
> `{"returncode": N, "output": …}` envelopes before
> `classify_step_error` (new shared `split_envelope` in
> `trajectory_error_taxonomy.py`, also adopted by `trial_diagnosis`
> for its local envelope parse). Harness `extra.exit_code` stays
> authoritative; the envelope fills only when extra carries none.
> Trial counts: harness-pilot 49, HAR-71 4, exp05 view 20, lab runs
> 140 (read-only) = 213 trials, before vs after on identical inputs.

## Field changes (before → after, 213 trials)

| Field | Trials changed | Direction |
|---|---|---|
| `total_errors` | 49 | 0 → N (all harness-pilot envelope trials) |
| `recovery_count` | 47 | 0 → N |
| `expected_probe_count` | 16 | 0 → N (probe misses now distinguished, not errors) |
| `step_to_first_error` | 48 | null → step id (one trial: step 13 → 6, earlier real error) |
| `time_to_first_error_seconds` | 48 | null → seconds (one trial corrected as above) |
| `recovery_latency_steps` / `_seconds` | 43 / 46 | null → values |
| `unrecovered_at_terminal` | 34 | false → true (all failed envelope trials) |
| per-step `error_category` / `is_error` | 49 (step digest) | `none` → `command_nonzero_exit`, `runtime_exception`, `file_not_found`, …, plus `expected_probe_miss` |
| heuristic label | 40 | 25 `failed_verification` → `unrecovered_error`; 15 `clean_success` → `recovered_success` |
| `intervention_category` | 0 | unchanged everywhere |

Zero changes on Terminus, exp05-via-Reef, and lab-runs trials: the fix
only affects the envelope transport. Zero crashes both runs.
`trial_diagnosis` modes are byte-identical on all 46 audited shell
trials (only the intended `error-count` improvement differs);
`heuristic_label` strings follow the corrected outline as documented
(detector v3).

## Consumers checked

- `interpretation/feature_registry.py`: registered feature names/types
  unchanged (values recompute).
- `interpretation/traj_baseline.py`: provenance formulas unchanged
  (values recompute); baseline Parquet is derived, not immutable.
- `TRAJ_FEATURES_PARQUET_SCHEMA`, `sql/traj_views.sql`, dashboard
  explorer: column names/shapes unchanged; views aggregate.
- `labels.py` heuristic routing: the 40 flips above are the fix working
  (both directions move toward truth); human labels untouched.
- `interpretation/evidence_pack.py`: builds from IR baseline metrics,
  not the outline path — unaffected.
- Root `trajectory_ir.py` has the same envelope blindness in its own
  counting; deliberately out of scope (separate module, doubles the
  radius; candidate follow-up).

## Decision

Merge (no migration): schema unchanged, no immutable evidence touched,
all derived tables recompute, both heuristic flips move toward truth,
and `docs/NOW.md` already lists error-timing emptiness as a dated
observation this resolves rather than a contract anyone relies on.
