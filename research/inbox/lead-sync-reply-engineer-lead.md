---
source_type: internal
---

# Lead sync reply — engineer-lead (overnight session)

1. **Current focus:** Live-firing the metered zai-opencode lane end-to-end (screening k-series on `registered/syn-funcdag-easy`); branch `feat/next-buildout-report`; just fixed proxy 401 root causes (OpenAI-style paths, bare model IDs, auth.json-not-env credential channel proven vs live OpenCode 1.18.25); k1h dispatched, watching outcome.

2. **Top blocker:** None waiting on Peter (approval requirement removed by his standing decision today) or any pane. Current technical blocker is mine: model-call 401 chain (nearly closed) plus an early SIGTERM/trial_wall_clock_timeout seen on one attempt — root cause not yet isolated.

3. **Unowned item for a generalist:** Executor failure hygiene in `src/evallab/queue.py` (`_dispatch_one` + `reconcile_running`): (a) a dead child leaves specs orphaned in `queue/running/` with no auto-reconcile to failed; (b) resubmitting a failed spec preserves `spec_id`, creating duplicate records across states that break `locate()`'s exactly-one invariant; (c) `run_experiment` refuses reused job dirs with `FileExistsError` instead of archiving aside. Contract to defend: every dispatch attempt ends in exactly one terminal queue record, and no orphaned `running/` entry survives a tick. Touch nothing in lanes/policies — pure executor robustness + behavioral tests.
