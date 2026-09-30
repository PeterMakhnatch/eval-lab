# Reservation calibration: byte-length bound → divisor 2 (HAR-114 follow-up)

$0 record. Produced by `calibrate_reservation.py` in this directory
(`uv run --no-sync python calibrate_reservation.py`); machine-readable
numbers live in `reservation-calibration.json`. Read-only over the
evidence worktrees; no trials, no deploys, no model calls.

## 1. Why not a tokenizer

The proxy container is stdlib-only: the compose files pin
`python:3.12.11-slim` by digest, mount the proxy script read-only, and
install no pip dependencies. Serving an exact tokenizer would require an
image rebuild (vendored `tokenizers` + a `tokenizer.json` pinned by
digest, served offline) for a security-critical metered path, and the
served MiMo distill has no published exact tokenizer artifact to pin —
it is Qwen-derived, so any third-party Qwen tokenizer would be an
approximation, not a bound. The calibrated divisor keeps estimation
dependency-free (pure arithmetic on locally computed bytes), which is
fail-closed by construction: there is no tokenizer path that can go
missing and no smaller fallback. The byte length itself remains the
ultimate fail-closed bound if the divisor is ever removed.

## 2. Calibration corpus

All 82 runs in `runs.jsonl` (HAR-110/104/81). Per settled call, the
job-level `lab-metadata.json` `provider_usage.calls` ledger carries both
`reserved_input_tokens` (the proxy's byte-length estimate) and the real
`input_tokens`: 6,871 settled pairs, 28 unresolved (reservation held,
excluded from ratios), 0 zero-usage. Every call served
`selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. One job name covers
two records with distinct ledgers (live job + aborted leftover); both
ledgers are included exactly once.

## 3. Distribution and chosen bound

`reserved / actual`: min 2.614, p01 2.802, p05 3.022, median 3.560,
mean 3.580, max 4.796.

New estimator: `ceil(billed_json_bytes / 2)`. Over all 6,871 calls the
new bound stays >= actual on 100%: 0 under-reservations. Worst-case
headroom is 2.614 / 2 = 1.307 (~31% above the tightest observed call).
Divisor 3 under-reserves 299 calls and is rejected.

The tightest calls cluster in `har81-p-d-arvo-42514310` (fuzzing task):
prompts stuffed with hex-digest corpus filenames, which BPE packs
densely (~2.6 bytes/token). The bound's justification beyond the fit:
the billed JSON is overwhelmingly ASCII (roles, JSON punctuation,
English prose, code, terminal output), whose Qwen-family rate is ~3–5
bytes/token; the observed floor is set by hex/numeric-dense output, and
divisor 2 keeps a 31% margin below even that. Adversarial floor (pure
random bytes at ~1 byte/token) is outside this workload's distribution;
if content ever shifts there, the ledger still records both reserved and
actual per call, so drift is detectable, and every reconcile trues up —
the gate can trip at most about one call late.

## 4. Stop replay: old vs new

66 runs ended `trial_budget_exhausted` (59 input-ceiling, 7
request-ceiling). Replaying the input gate
(`settled + held + next_reservation > 2.5M`, last settled call as the
next-call proxy) explains 56/59 old stops; the other 2 tripped on a
larger next prompt than their last recorded call. Median settled input
at the old stop: 2,389,635.

Under the new estimate (held and next reservations also halved, as they
would be under the new rule), 59/59 input-exhausted runs continue past
their observed stop — the new stop point is beyond the recorded
trajectory and therefore a counterfactual (median freed on the next
reservation alone: ~67k tokens). The 7 request-ceiling runs are
unaffected in call count (their stop counts calls, not tokens).

## 5. Limits

- The bound is empirical over one model and one workload family
  (Terminus agent prompts); a new model or radically different content
  (e.g. CJK-heavy or byte-dense prompts) needs recalibration.
- Unresolved calls (28) hold reservations by design and are untouched.
- Output reservation (`max_output_tokens`) is unchanged.
