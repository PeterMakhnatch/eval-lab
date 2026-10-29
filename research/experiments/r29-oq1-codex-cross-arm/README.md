# R29-OQ-1: Codex arm on the frozen OQ-10 unseen cohort

`codex_arm.py` sends the exact frozen study-v2 prompt bytes (pin-checked) to `openai-codex/gpt-6.1-terra`
(system text, `thinking high`, no tools, controlled config unchanged), once per slot, and builds each cell with the
frozen parser `study.parse_flags`. The frozen native transport only routes Sonnet/Haiku/Kimi, hence the side driver.
A slot counts only if every assistant record is served by terra, with no fallback and no tool call; Codex 7-day
usage >= 85% or a STOP file halts new sends. `--selftest` replays stored Sonnet responses through the cell builder
(flags must match the frozen cells) and makes one DEV-prompt smoke call.

`score_arm.py` scores once with the frozen scorer for roles {sonnet, codex}; it aborts unless the Sonnet view/gate
scores and record digest equal the earlier retained result. Codex and Sonnet are separate strata, never pooled.

Task-label transfer, not causal-bug replication: AutomationBench unchanged tasks are not certified clean (alarms
there are "unverified alarms"); PAIChecker "misaligned" is a human-alignment construct. Receipts and numbers:
bridge `opus-vs-swarm-qa.md`, section R29-OQ-1.
