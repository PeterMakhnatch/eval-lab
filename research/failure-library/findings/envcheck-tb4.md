# envcheck-tb4

Rebuild: `uv run evallab failures ingest envcheck-tb4`.

- Source: [Tokenless AI / EnvCheck findings](https://github.com/tokenless-ai/envcheck-findings), commit `12d4d094ef61c4898c16d18ebcc17beafce86035`, post `2026-10-grader-defects`. Raw `data/findings.jsonl` SHA-256: `893001c93945bd063ed2565145d846eb0e026a030f719ce4295ecebdde9039c9`.
- Local custody: `<state>/raw/envcheck-tb4/12d4d094ef61c4898c16d18ebcc17beafce86035/findings.jsonl`, immutable and hash-checked. Intake reuses retained local bytes before a free pinned GitHub download. `<state>` defaults to `~/.local/state/evallab-failure-library`, configurable with `--state-root` or `EVALLAB_FAILURE_LIBRARY_STATE`.
- Key: `research/answer-keys/envcheck-tb4-39/items.jsonl`, SHA-256 `19bf4ff9d950eb6bf68a52031c25b9c7d918ae567397c16d3f877309db78da82`. Epoch overlay `epoch_flags.json` SHA-256: `c7eb519631c35bfb3f8feb56a573225ec9dff5e3ee77b9b70458d15a694964b4`.
- Task pins are copied from the labelled packages, expanded to full SHAs: `v4.0.0` = `452bf305c6daa62fc59061d22133a7cbc7c1572e`; `1dcda8716784` = `1dcda8716784493721921c23e4bc7f7d988b4494`, both in `harbor-framework/terminal-bench`. Per-task package digests are retained.
- Count: **36 findings on 25 tasks**: 24 EnvCheck finding slugs and 12 issue-read [Epoch AI flags](https://epoch.ai/benchmarks/terminal-bench-4/review). The fourteen sampled weak negatives produce no finding. The wider Epoch table is not silently transferred to a different task revision.
- Tiers: **19 V3_executed**, **12 V1_external_label**, **5 V0_unverified**. V3 requires a task-matched, explicitly incorrect accepted case with graded reward 1 and a published reproduction receipt path/hash. Epoch issue claims are `human_label` / Epoch AI, not execution.
- The five V0 cases are intentionally not promoted: three checker-only demonstrations, a plaintext-readability demonstration without a passing wrong submission, and the ATRX result whose intended verdict depends on an ambiguous fragment-local versus locus-global reading. Existing source labels are preserved as claims, not overstated as full-task reward-hack confirmations.
- Mapping: `weak` → TS01, checker-logic family → TS02, `reachable` → TS03, `tamperable` → TS04, `prompt_misaligned` → TS06; an unspecified `broken` category stays `unclassified`. Epoch false negatives get `unfair_fail`; other admitted source mechanisms target `wrong_pass`.
- Locator: EnvCheck finding slug or Epoch issue ID. Existing per-task key splits are copied unchanged: **8 DEV / 28 heldout** findings. No new assignment rule is applied; cross-key admission remains separate.
- License: **CC-BY-4.0**, with attribution to Tokenless AI and Epoch AI. Task code retains its upstream license. No source reproducer code or raw logs are added to Git.
