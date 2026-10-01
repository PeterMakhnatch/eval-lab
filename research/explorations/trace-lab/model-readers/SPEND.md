# Model-readers spend ledger (cap: Docent $0, ZAI ≤ $2.00)

ZAI Open Platform prices (https://docs.z.ai/guides/overview/pricing):
glm-5.3 $1.40/1M in, $4.40/1M out (cached input $0.26/1M). Cost =
in/1M*1.40 + out/1M*4.40, upper bound (no cache credit). Per-call usage
from each scan's `scan_model_usage` (summed over ALL rows — an early
version of this ledger wrongly used only the first row; corrected
2026-10-01).
Docent hosted readings: $0 (all models `uses_byok=false`; no billing signal).

> NOTE: the `--worklist` bare-array file did not filter (scan `_scan.json`
> lists all 12 transcript ids), so the "batch 1" scan IS the full 12-run
> scan; no batch 2.

## Actuals (all numbers from scan/reading usage records)

| when (UTC) | surface | calls | in tok | out tok | reasoning tok | cost USD | running total |
|---|---|---|---|---|---|---|---|
| 2026-10-01 | inspect_ai smoke `glm-5.3-flash` ("OK") | 1 | 17 | 114 | — | 0.0005 | 0.0005 |
| 2026-10-01 | inspect_ai smoke `glm-5.3` ("OK") | 1 | 17 | 52 | — | 0.0003 | 0.0008 |
| 2026-10-01 | scout validation v1 `glm-5.3` (2 HAR-104 trials, prompt v1) | 2 | 85038 | 48550 | 47013 | 0.3327 | 0.3335 |
| 2026-10-01 | scout validation v2 `glm-5.3` (2 HAR-104 trials, frozen prompt) | 2 | 85062 | 29573 | 28275 | 0.2492 | 0.5827 |
| 2026-10-01 | scout FINAL `glm-5.3` (12 HAR-119 runs, frozen) | 12 | 489045 | 236838 | 227847 | 1.7268 | 2.3094 |
| 2026-10-01 | docent draft validations (2×1 HAR-104 run, opus-5-5) | 2 | 134659 | 3772 | — | 0.00 (hosted free quota) | 2.3094 |
| 2026-10-01 | docent FINAL `anthropic/claude-opus-5-5` (12 HAR-119 runs, frozen) | 12 | 900566 | 26745 | — | 0.00 (hosted free quota) | 2.3094 |

**ZAI total: $2.31 — $0.31 over the $2.00 cap.** The overrun came from
(1) under-accounting: the ledger first summed only each scan's first
row, missing half the validation calls ($0.23 unlogged); (2) heavy
reasoning outputs: 228k of the final's 237k output tokens are thinking
tokens at $4.40/1M. No further ZAI calls will be made: the final scan
returned 12/12 with 0 errors, so no retries are needed. With cached-input
credit (58,264 cached tokens at $0.26/M) the total is $2.24 — still over.
Reported honestly here and in the README; the $0 Docent cap holds.
