# Cheap-model cost-quality frontier (CM-1, 2026-10-21)

This experiment adapts the **static/plain-read arm of #1119** to hosted,
OpenAI-compatible models. It retains the original `reader_prompt.md`, released
taxonomy, eleven classes, eight-finding limit and canonical scorer. It is not an
exact replication of the subscription-agent transport: models get bounded
read/grep JSON actions instead of shell access, one fresh context per task, and
no task code execution. Do not call this adapter harness v1 (#1121).

## Custody and safety

`run_plain.py --prepare` creates an owned copy of the released public agent view,
checks every declared SHA-256, and runs the **unchanged** sealed-view validator.
The shared TEST tree has two unmanifested SQLite sidecars; only the declared
bytes enter the owned copy, and excluded paths are recorded. Shared inputs are
never changed. No QA finding labels, private manifests, TEST trajectories or
scorer results enter a model prompt. Released reference solutions/tests remain
legitimate static audit inputs.

Models only inspect source through `WorkspaceTools`; no shell, task execution,
network tools, exploit execution or host writes are offered. Package text is
untrusted evidence. A static finding is not a confirmed exploit.

## Frozen models and cost guard

`models.json` records four exact provider slugs, public price-source URLs and
reasoning settings. Fallbacks are forbidden. DeepSeek explicitly uses
DeepInfra FP8: the historical first-party slug was absent from the live endpoint
catalog. This is not evidence that the historical first-party route works.

Every request is durably reserved **before** networking in one campaign-wide
`cost.jsonl`, under a file lock. Full JSON request UTF-8 bytes plus 1,024 framing
tokens and maximum completion tokens are priced at pinned uncached rates. The
lifetime ceiling is **$4 across all configurations/protocols**, not $4 per model.
Failures and missing billing retain their full reservation. A successful free
OpenRouter generation-metadata GET may settle a call using the matching request
ID/model's reported `total_cost`; estimates are never labelled billed dollars.
An observed cost above its reservation durably holds the entire campaign.
No inference request is retried automatically.

Raw requests, responses, generation receipts, per-task status and dispatch
claims live outside git under:

`~/Developer/eval-lab-results/2026-10-21/cheap-model-frontier/`

## Execution

Use the owned worktree's locked Python environment and inject credentials with
`keys run`; never print the key.

```sh
.venv/bin/python research/experiments/cheap-model-frontier-20261021/run_plain.py --prepare
keys run -- .venv/bin/python research/experiments/cheap-model-frontier-20261021/run_plain.py --model gpt-oss-120b --split dev
keys run -- .venv/bin/python research/experiments/cheap-model-frontier-20261021/run_plain.py --model gpt-oss-120b --split test
```

Repeat for the other explicitly listed model slugs, within the **same** output
root and shared ledger. Each configuration first measures the three declared
DEV smoke tasks (not the whole 25-task DEV split). All must finish before its
single 41-task TEST run is admitted. Freeze records bind source, prompt, model,
limits and both public-view manifests. Exclusive task and TEST claims refuse a
second dispatch; interrupted TEST runs are not relaunched. Missing/failed or
abstained tasks remain explicit coverage limitations, never clean negatives.
Log actual TEST exposure in the answer-key split registry before publishing a
score. Do not tune prompts, providers or limits using TEST output.

`run_v1.py` supplies a JSON transport to the **unchanged #1121 Session host**.
It reuses the original prompt, playbooks, source-only tools, evidence admission
and 60-tool/900-second limits; no second host or execution-enabled v2 is substituted.
An isolated loaded module binds the owned sealed views and actual reviewer model
without editing historical source or artifacts. It uses the same model pins and
request-before-network financial adapter as plain read. Run the same DEV/TEST
commands with `run_v1.py` for that second protocol. Task QA retains ownership of
`qa/env*`, `qa/agent*` and the separate v2 development.

The legacy v1 finding schema stores a host placeholder `reviewer.cost_usd=0`.
It does **not** meter API inference. Preserve the unchanged host contract, flag
that field as unmeasured in task status, and use only the generation-bound
campaign billing journal for all cost claims.

## Interpreting the eventual comparison

Use the unchanged `tb4-audit-v1/score_audit.py` one-to-one matcher. The historical
Opus references are 78/152 (plain) and 80/152 (v1) TEST category matches, not
runtime-confirmed exploit counts. Subscription marginal metered spend is not
an API-equivalent economic price. Tokenless Eval3's Elo and total 65-environment
spend are external context, not convertible to this finding-recall metric:
https://usetokenless.com/eval3/ . Its local source copy is
`research-context/sources/links/usetokenless.com/2026-10-20-eval-leaderboard-tokenless.md`.

`score_frontier.py` converts reader finals with the original class/category
mapping, loads v1's validated findings, and invokes that unchanged scorer. It preserves failed,
missing and abstained coverage, separates DEV from TEST cost, and omits runs
with incomplete coverage or unknown billed cost from the dollar frontier.
Run it only after configurations are frozen and their TEST exposures recorded:

```sh
.venv/bin/python research/experiments/cheap-model-frontier-20261021/score_frontier.py
```

The output is `scoring/frontier.json` plus an SVG. The historical Opus counts
are horizontal references, not invented API-dollar coordinates. Seven-figure
token prices are fixed before DEV; actual plotted spend comes from per-request
generation billing, not those price estimates. A valid `cannot_tell` completes
a DEV smoke but remains an explicitly labelled abstention in TEST reporting.
