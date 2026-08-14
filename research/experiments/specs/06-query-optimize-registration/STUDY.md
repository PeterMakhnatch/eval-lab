# Study 06 — Codex on lab-authored query-optimize (registration probe)

**Hypothesis.** query-optimize is a distinct lab-authored family (SQL
rewrite + performance gate) and is not in the canary suite. A Codex
pass@3 on it would extend the lab baseline past the three pinned
canaries.

**One variable.** Task membership: `tasks/query-optimize` versus the
pinned `canary/*` set. This spec asks the policy engine that question
directly by using the real path, not a `canary/` or `registered/` alias.

**Fixed.** `agent=codex`, `attempts=3`, docker, `est_cost_usd=2.50`.

**Policy.** No standing rule matches `tasks/query-optimize` for a
billable agent. Expected reason: `out_of_policy`. Admitting it would
require Peter to either add it to `policy/canary-suite.yaml` (digest +
version pin) or create a `registered/*` name and satisfy
`researcher-followups` requires.

**Task facts (not results).** Medium SQL optimization; verifier timeout
1800s; agent timeout 900s; image `alexgshaw/query-optimize:20251031`;
performance test is a 5-iteration timed comparison against a golden
query. That makes it a poor canary (slow, image-pinned, timing-sensitive)
and a reasonable first `registered/*` target if Peter wants a harder
slice.

**Free baseline.** Oracle/nop on this family run via `harbor-lab matrix`
in `baselines/query-optimize-controls.json`. Those controls test task
validity only.

**Next spec this implies.** If Peter registers it, resubmit as
`registered/query-optimize` with the three requires once those checks
exist. If the oracle baseline fails, do not register it.
