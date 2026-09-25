# Morning packet: noise-floor specs (parked, never approved)

Prepared 2026-09-25 by the LoopPool worker on `feat/reef-loop-pool`
(worktree `.worktrees/reef-loop-pool-20260925`). **Nothing here has been
approved or executed.** Every spec below sits in `queue/waiting/` of that
worktree and will not dispatch until Peter records an authorization.

## What is parked

One pinned Terminus 2 harness tree × 4 dev tasks × 5 repeats = **20 specs**,
one trial each (`terminus-2` specs bind exactly one trial by contract).

- **Tree:** `research/experiments/reef-loop-pool-20260925/harness-tree-baseline`,
  a byte-identical copy of HAR-71's baseline tree
  (`sha256:058bb47499fd5a9f64e315bcc03e394ca8998b8494f2cfc5442cd27877fe286d`).
  Justification: it is the only tree with merged-revision execution proof
  (HAR-71 receipt: 4 native runs, verified skill locks for `inspect-files` and
  `verify-output`), so the noise floor measures model/task variance, not new
  harness content.
- **Tasks:** the proposed dev pool — exp05 search `fix-median`, `sales-total`,
  `error-count`, `top-words` (package digests in `split-proposal.json`).
- **Model route:** metered `zai/glm-5.3-flash` (standard-API
  `ZAI_OPENAPI_API_KEY`), `--environment docker`, `--cost-limit-usd 0.40`
  per trial. Estimated spend from HAR-71's token envelope
  (15–20k input / 2–3k output per trial at $0.15/M in, $0.50/M out):
  ≈$0.004/trial, ≈**$0.08 total**, far under the $3/job and $20/day ceilings.
  Wall time is not yet measured on this pool [INFERENCE]; HAR-72's DeepSeek
  A/A ran ~1 s/episode on far smaller tutorial tasks, exp05's local 30B ran
  56–119 s/episode, so expect minutes per trial and roughly 20–40 min for all
  20 at the queue's serial dispatch.

## Spec ids

| Spec id | Name |
|---|---|
| 01M3BJ7WZB9XEH62S13GY886PX | loopool-noise-error-count-r1 |
| 01M3BJ7XFYJPP6E2HY5N7228WF | loopool-noise-error-count-r2 |
| 01M3BJ7XYPC8G5CC3HNQCV89EJ | loopool-noise-error-count-r3 |
| 01M3BJ7YDCSDM6M1RR8PPE6KN4 | loopool-noise-error-count-r4 |
| 01M3BJ7YWQ8Q1BJGFD2M9YD2NJ | loopool-noise-error-count-r5 |
| 01M3BJ7ZBVAP4WGSBN9ZHDGX07 | loopool-noise-fix-median-r1 |
| 01M3BJ7ZV4WPNPSZZJNY8Q7PEN | loopool-noise-fix-median-r2 |
| 01M3BJ80A8XDSCA87Z88WG8TMA | loopool-noise-fix-median-r3 |
| 01M3BJ80S95DJPDSXKE6934NYX | loopool-noise-fix-median-r4 |
| 01M3BJ8189CXZ6NQVWJ9S848BJ | loopool-noise-fix-median-r5 |
| 01M3BJ81R4M7HWGNM065QB7QAW | loopool-noise-sales-total-r1 |
| 01M3BJ827X4309K5SHQS2JB66N | loopool-noise-sales-total-r2 |
| 01M3BJ82QWZN9NKG468Y678ZD1 | loopool-noise-sales-total-r3 |
| 01M3BJ83779Y71KKY31ER26QQJ | loopool-noise-sales-total-r4 |
| 01M3BJ83PPHTHETVXKJHK6CVGB | loopool-noise-sales-total-r5 |
| 01M3BJ845YXW1KBQNH2YW95W7F | loopool-noise-top-words-r1 |
| 01M3BJ84N2RH5CD6GSX67EF7DE | loopool-noise-top-words-r2 |
| 01M3BJ85472WJG6MZRDEYPQZFT | loopool-noise-top-words-r3 |
| 01M3BJ85KJ2ZG1HD33AP7J03D0 | loopool-noise-top-words-r4 |
| 01M3BJ862F4ED0CK1ETAXPD0KV | loopool-noise-top-words-r5 |

## Peter's commands (from the worktree root)

```bash
cd ~/Developer/eval-lab/.worktrees/reef-loop-pool-20260925
# review a spec if desired (prepared copies also live in derived/prepared/):
cat queue/waiting/01M3BJ7WZB9XEH62S13GY886PX.json | python3 -m json.tool
# authorize (each, or in a shell loop over the table above):
uv run evallab approve 01M3BJ7WZB9XEH62S13GY886PX --actor peter
# dispatch exactly these (repeat per spec, or `uv run evallab tick` to drain):
uv run evallab tick --spec-id 01M3BJ7WZB9XEH62S13GY886PX
```

Requires `ZAI_OPENAPI_API_KEY` exported in the dispatching shell (standard API
key, never Coding Plan credentials).

## Why the metered route (route inventory)

Terminus 2 routes that exist in main tonight, exactly two
(`src/evallab/execution_contracts.py`, `harbor_terminus.py`):

1. **`zai/glm-5.3-flash`** — metered standard-API route via the loopback
   secret proxy. Chosen for the packet: no local service dependency, explicit
   per-trial ceilings, known pricing.
2. **`ollama_chat/qwen2.5:7b`** — local route via `EVALLAB_TERMINUS_OLLAMA_URL`.
   The local selector admits **exactly** `qwen2.5:7b`
   (`terminus_local.py:69-70` refuses any other string; the metered branch
   requires exactly `zai/glm-5.3-flash`), reads Ollama's installed-GGUF
   inventory, and never pulls weights. Checked tonight: no Ollama answers on
   127.0.0.1:11461 or :11434, and Peter told another agent to stop restarting
   it — so the local route is down, and even up it gives qwen2.5:7b, which
   passed 0/4 in HAR-71 and would measure a floor, not a noise floor.
   qwen3-coder:30b (exp05's 2/4 model) is **not admittable** by the current
   local contract; widening the selector is runner work owned by ReefTraffic.

**What a DeepSeek or GLM-Flash Terminus route would require (not built):**
`SecretSafeTerminus2` hard-requires `zai/glm-5.3-flash` on the metered branch;
a DeepSeek V4.1 flash host-side route would need (a) a new allowed model
binding in `harbor_terminus.py`, (b) DeepSeek credential handling mirroring the
ZAI standard-API pattern (`execution_contracts.py` already carries
`DEEPSEEK_*` proxy constants for the mini-swe container-side lane, not
Terminus), and (c) proxy-ledger pricing entries for `deepseek-flash`. That is
ReefTraffic's file ownership; this packet does not touch it.

## What the run answers

Per-task pass rates at k=5 for glm-5.3-flash on the proposed dev pool: the
empirical rates the gate planner needs (replacing exp05's single-episode
rates), plus run-to-run variance of one fixed tree — the A/A noise floor
HAR-73's gate configuration will read.
