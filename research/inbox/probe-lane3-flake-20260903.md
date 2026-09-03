# PROBE: why did lane-3 evals flake on Sep 2?

**Claim:** analysis #2, board.md:46. **Prober:** wS:p9 (zai/glm-5.3-flash), Sep 3.
**Verdict: ROOT-CAUSED — Z.ai secret-proxy path allowlist rejected the OpenAI-style
chat path (`404 endpoint not allowed`), the agent exited 1, and every trial scored
0.0. The same-night fix (`1ec49c13`) was never merged to `main`, so the failure
condition is still latent on main for every `zai-opencode` run.**

## Evidence chain (digests + file:line)

1. Flaking runs (gitignored `runs/`, both present Sep 2 evening):
   - `runs/baseline-funcdag-easy-zai-opencode-k3` (21:47): `result.json` —
     `n_completed_trials: 0, n_pending_trials: 3, evals: {}` (abandoned attempt).
   - `runs/baseline-funcdag-easy-zai-opencode-k3-r2` (22:12): `result.json` —
     3/3 trials `n_errors: 3`, reward mean 0.0, `exception_stats:
     {"NonZeroAgentExitCodeError": [all 3 trials]}`.
   - Per-trial `exception.txt` (e.g.
     `baseline-funcdag-easy-zai-openco__CzXSt2x/exception.txt:22,62`):
     `harbor ... NonZeroAgentExitCodeError: Command failed (exit 1)` with stdout
     `{"type":"error", ... "APIError" ... "statusCode":404,
     "responseBody":"endpoint not allowed",
     "metadata":{"url":"http://zai-secret-proxy:8080/chat/completions"}}`
     (harbor `single_step.py:77 → base.py:851`; adapter
     `src/evallab/harbor_zai_opencode.py:313` in the traceback).
2. Proxy-side defect: `containers/zai_secret_proxy.py:41` (at the time)
   `ALLOWED_PATH = "/api/paas/v4/chat/completions"`; `do_POST` at :388-391
   rejects any other path with `404 endpoint not allowed` (witnessed bytes match
   `_reject` message exactly). opencode's OpenAI-style client posts
   `<baseURL>/chat/completions` because the adapter sets
   `baseURL = http://zai-secret-proxy:8080` (bare —
   `src/evallab/harbor_zai_opencode.py:59,252,279-280`).
3. Same-night fix, **never merged to main**: commit `1ec49c13` (Sep 2 22:15,
   "Accept OpenAI-style chat paths in Z.ai proxy") adds
   `ALLOWED_CLIENT_CHAT_PATHS = frozenset((ALLOWED_PATH, "/chat/completions", ...))`
   and relaxes the check at :390. `git branch --contains 1ec49c13` → side branches
   only (`analyst/capability-deficit-miner`, `feat/curriculum-candidates`, …);
   **absent from `origin/main`**, where `containers/zai_secret_proxy.py:41,390`
   still enforce the single strict path.
4. Why it looked transient: the k3/k3-r2 failures triggered a Sep 3 morning
   repair chain on the auth side (`64142e8c` 10:46, `ffd7a561`/`008f04a7`
   10:52-10:57 capability delivery, `9c87bb2c` 11:10, `7a9881d6` 11:24 bare model
   IDs) — those fixed a *second*, distinct failure layer (401 capability/model
   ID rejection), masking the still-latent path issue. Free `oracle` probes
   (e.g. `runs/oracle-probe-funcdag-capability-fix`, 10:29, reward 1.0) bypass
   the proxy entirely and cannot exercise this path.
5. Corroboration (wS:pA pointer, independent): same run identified; job UUID
   `56fe0cbf…` later surfaced in MissingCASAuthority failures — consistent with
   the auth/proxy cascade, not with task or verifier faults.

## Classification

`harness_failure` (proxy allowlist vs adapter base URL), not `task_invalid`,
not `verifier_false_negative`, not genuine agent failure — the agent never got a
model response (404 on first request).

## Residual risk + recommended repairs (proposals only, not committed)

- R1 (one-line semantic): merge `1ec49c13`'s `ALLOWED_CLIENT_CHAT_PATHS`
  acceptance (or set the adapter/provider baseURL to
  `http://zai-secret-proxy:8080/api/paas/v4`) on **main**. Until then, every
  `zai-opencode` trial on main is a deterministic 404 → 0.0.
- R2 (lint): a probe test asserting `_presented_capability`/path acceptance
  parity between the adapter's configured URL and the proxy allowlist, so the
  pair cannot drift silently again.
- R3 (observation): first-request failures should be classified distinctly from
  capability failures (trial produced no model turn) so this class is visible
  as infrastructure, not reward 0.0.
