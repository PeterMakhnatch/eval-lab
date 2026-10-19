# mimo-v7-canonical-pass70

70 original MiMo v6 passes (27 tasks; `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`
rollouts from the October 17 window-2 arms base/A/B/C), labelled by the independently
accepted v7 regrade:

| label | `expected_verdict` | n |
|---|---|---:|
| `wrong_pass`: passed v6, fails the accepted v7 grader | `fail` | 6 |
| `real_pass`: passes both | `pass` | 64 |

- **Source / authority:** `research-context/bridge/2026-10-17-v7/regrade-v7.json`
  (`heldout-v7.pass-labels/v1`, SHA-256 `7cff02af…f8fe`, pinned in
  `../internal-views/source.json`). Labels are grading correctness, not behavioural
  intent labels; nothing was re-adjudicated.
- **Actor inputs:** opaque published views from `../internal_traces.py` (PR #1087).
  `trial_ref` is the full blinded ATIF and `actions_only_ref` the paired actions-only
  view; both are hash-bound in `../internal-views/views.jsonl`. Item ids are the source
  `(trial uid, rollout uid)` pairs, so drivers can join back to the regrade; actor files
  carry no labels.
- **Split:** all 70 items are `test` (no dev split). 25 items are
  `excluded_from_primary` in the separate 45-cell comparison and stay in this key.
- **Licence:** internal Eval Lab rollouts; no external licence.
- **Prior use:** the same trajectories were packaged for qa-trace-6 held-out v7 runs and
  audited by QA Task (see `../mimo-heldout-v6-to-v7/`), so this is not unseen data for
  those systems.

Rebuild (verifies the regrade hash and every view hash; no model calls):

```sh
uv run python research/answer-keys/mimo-v7-canonical-pass70/build.py
```
