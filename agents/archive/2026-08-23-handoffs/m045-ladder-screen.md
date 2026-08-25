Status: done
Last: merged as PR #140 (`1064a31`)
Next: none
Blockers: none

# M045 LADDER Screen Handoff

The screen is one explicit `screen_id`/`grid_id` cohort. `ScreenSpec` accepts registered task IDs, ordered model levels, `initial_k`, `followup_k`, `expected_baseline`, and decision rules. Duplicate task or model-level names are rejected; at least two ordered levels are required.

## Operator flow

1. `uv run evallab ladder screen stage1 <screen.yaml> -o queue/proposed` emits one k=`initial_k` `ExperimentSpec` per registered task/model level. Each spec carries `grid_id`, `grid_point.screen_id`, stage, task, model level, agent/model, task version, verifier digest, preregistration, power plan, and submitter provenance. It writes proposals only; no model is dispatched and human approval remains required.
2. `uv run evallab ladder screen analyze <screen.yaml> --jobs-dir runs` reads screen-provenanced trial `result.json` records. Catalog-style fact fields (`primary_reward`, `exception_class`, `exception_type`, `agent_name`, and `model_name`) are also accepted through the analysis record boundary. Stage 2 results are excluded from Stage 1 analysis.
3. `uv run evallab ladder screen stage2 <screen.yaml> -o queue/proposed --jobs-dir runs` emits k=`followup_k` specs only for tasks classified `separating`. It never auto-approves or dispatches paid work.

Analysis classifications and operator actions are explicit:

- `saturated-pass`: every ordered level is at/above the pass threshold; stopped for ceiling saturation.
- `saturated-fail`: every ordered level is at/below the fail threshold; stopped for floor saturation.
- `separating`: ordered-level spread meets `min_separation_delta`; selected for k=3 follow-up.
- `broken/error`: any level has an execution or harness exception; stopped until the task/run is repaired.
- `insufficient`: a level is missing, has no reward, has insufficient spread, or violates required monotonic ordering; stopped with the exact missing/decision reason.

Existing queue/evidence points are deduplicated by exact cohort ID, task, agent/model, preamble, and k. Unrelated queue comparisons and same-prefix job names are not pooled.

## Difficulty variants

`DifficultyVariantContract` records the deterministic authoring boundary and rejects verifier-changing or boundary-bypassing variants. No prose-only difficulty mutation is generated. Real task mutations must be authored, verifier-preserving, explicitly registered, and then screened through the same flow.

## Verification

- `uv run pytest -q tests/test_ladder_screen.py` — 18 passed.
- `uv run pytest -q` — full repository gate passed (one expected xfail and one skip).
- `uv run ruff check src/evallab/screen.py src/evallab/power.py src/evallab/ladder.py src/evallab/cli.py tests/test_ladder_screen.py` — clean.
- `uv run evallab doctor` — all required checks passed; it reported the shared derived-root checkout warning and exited 0.
- No paid model or cloud dispatch was run.
