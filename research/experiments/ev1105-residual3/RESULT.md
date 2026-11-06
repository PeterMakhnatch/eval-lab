# Residual3 result

**V3 passes the preregistered acceptance checks without another held-out evaluation.**

The 2 named parser defects no longer fire. All 6 residual DEV cases still receive
`L0_SOFT_PROTECTED_SYSTEM_WRITE` through actual production L0.
DEV cheat keeps remain 3/332, versus 9/332 on the production baseline.
All 358 honest final decisions remain unchanged. All 690 final decisions match v2.

## Registration and scope

Registration commit `16c10409d` is pushed before the first probe or DEV scoring call.
It permits exactly 2 finding exclusions: read-only existing `r+` handles and
Python statements after a direct unconditional process exit in the same block.
The implementation candidate is `16ae1c891fb91773d06980db552bc5270386b6b9`.
The production baseline is `737f9ff320993d1210f2d149c2d9b9b94cda505c`.
The frozen v2 reference is `1652b45bcd23fae00e76c69999af3c4662a3d235`.
The original closed PR #1917 and its branch remain historical evidence.

No held-out view is opened or scored in this lane.
No model calls, helpers, native subagents, fallback routes, paid compute, task-policy
changes, label changes, threshold changes, or J3 activation occur.

## Monotonicity proof

V3 preserves the v2 ordered action stream and every read/script-invalidation transition.
It adds only a finding-eligibility flag. Excluded actions cannot emit findings.
Each emitted v3 finding therefore has an identical v2 step, kind, and path.
Removing an action's finding eligibility cannot revive a saved script or erase a prior read.
This structural argument applies beyond the measured cases.

The differential property check confirms finding multiset containment over:

- All 23 v2 regressions, with 32 scanner comparisons.
- All 3 semantic-audit probes. Both defect probes stop firing; the real tee write remains reviewable.
- All 690 frozen DEV inputs. Both versions fire on the same 14 inputs, with 0 finding-count changes.
- All 6 real residual trajectories through the production L0 integration.

The DEV set includes 10 valid patch-only inputs. Neither version receives trajectory
steps for those inputs. The full-line gate still checks their final decisions.
No missing input or parser error is excluded from the comparison.

An additional synthetic check covers 11,979 generated Python programs.
Every program preserves the exact ordered v2 action stream when eligibility flags are removed.
The checks parse payloads as data. They never execute them.
These synthetic counts establish correctness properties, not population false-positive estimates.

## DEV results

`make line-gate` returns exit 0 and PASS in 214.26 seconds.
No corpus gains a cheat keep or an honest drop.
A separate identity-level audit confirms 0 honest final decision changes and
0 final decision changes relative to v2.

| Measure | Production baseline | V3 | Wilson 95% interval |
|---|---:|---:|---|
| DEV cheat keeps | 9/332 = 2.71% | 3/332 = 0.90% | Baseline [1.43%, 5.07%]; V3 [0.31%, 2.62%] |
| DEV honest decisions changed | — | 0/358 | [0.00%, 1.06%] |
| DEV honest keep / review / drop | 63 / 255 / 40 | 63 / 255 / 40 | Counts only |

Intervals describe individual runs. They do not correct for task clustering or DEV selection.
The residual 6 cases remain design evidence, not a held-out estimate.

## Inherited held-out bounds, not new measurements

V2 has 0/654 new MALT honest reviews and 0/157 new math-control honest reviews.
Its Wilson 95% intervals are [0.000%, 0.584%] and [0.000%, 2.388%], respectively.
V2 also has 0 new drops and 0 new-reason firings on each panel.
These values come only from the existing residual2 aggregate receipt.

Because v3 can only remove this soft-review reason, its new-review incidence
cannot exceed v2's incidence on either fixed panel. Both remain bounded by 0 additions.
This is a monotonicity consequence, not a fresh v3 score or new statistical sample.
The math panel already has 157/157 reviews and has limited power.
All inherited source and native-tool coverage limits remain.
No held-out end-to-end line replay is claimed.

## Verification and receipts

Executed local checks:

- The focused pre-push checkpoint passes all static/type gates and 100 tests.
- The combined scanner, hack-drop, and action-regression suite passes.
- The retained differential proof passes on actual production admission inputs.
- The 6-case real L0 smoke passes.
- The generated action-stream property check passes.
- The full DEV line gate and identity-level decision audit pass.

`monotonicity.json` and `line-gate-summary.json` contain aggregate evidence.
`prove_monotonicity.py` reproduces the required property check using DEV and synthetic data only.
The original v2 report and held-out aggregate remain under `../ev1105-residual2/`.
Raw DEV rows, audit outputs, gate receipts, and generated-program evidence remain private at
`/private/tmp/ev1105/residual3/`.

Delivery uses `uv run evallab ship`, exact-head hosted CI, the registered line gate,
protected merge, and a merged-revision differential smoke. The ship receipt records
the final head, tested merge revision, gate, merge commit, and cleanup outcome.
