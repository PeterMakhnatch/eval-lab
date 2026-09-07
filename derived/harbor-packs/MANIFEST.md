# derived/harbor-packs/ — committed pack manifest

The pack contents under this directory are **git-ignored** (re-downloadable
external corpora); this manifest is the committed record and is force-added
(`git add -f derived/harbor-packs/MANIFEST.md` — `/derived/` is a
directory-level ignore, so a `.gitignore` negation cannot re-include it).

**Binding contamination policy (precedent:
`research/external/harbor-index/README.md`):** these corpora are
**behavior-study material only**. Never capability claims — nothing derived
from them enters a lab capability number, a card's Result section, or any
comparison against lab-run trials. **No reward recompute** — imported outcomes
stay theirs, flagged external. **Fetch ≠ register** — a pack never auto-registers
a task; registration is human-only and separate.

## terminalworld

| Field | Value |
|---|---|
| Source pin | HuggingFace `EuniAI/TerminalWorld`, `sample` config (20-task random sample of the 200-task human-`verified` subset), paper arXiv:2605.22535 |
| Upstream revision pin | not yet recorded — capture the HF revision SHA on next re-fetch |
| Staged | `tasks/` (20 task dirs `tw_*`), `artifacts/*.tar.gz`, `data/*.jsonl.gz` |
| License | CC BY 4.0 (per-task `license` field still says CC-BY-NC-4.0 upstream; README §License commits to CC BY 4.0) |
| Sample rule | the official upstream `sample` split — no lab-side sampling |
| Validation observed | `tw_100459` `TaskModel.is_valid_dir` True; oracle trial `runs/harbor-pack-verify/2026-09-06__17-38-39` scored reward 1.0 (task checksum `0302169c…` in trial `result.json`) |
| Contamination class | public terminal recordings reverse-engineered into tasks; exposure unknowable per task; behavior-study only, external-flagged outcomes |

## facet

| Field | Value |
|---|---|
| Source pin | HuggingFace `FACET-Terminal/FACET-Terminal-Tasks-6k` (6,020 tasks), GitHub `StoKou/FACET-Terminal`, paper arXiv:2608.18580 |
| Archive digest | `FACET-Terminal-Tasks.zip` sha256 `7355ea41afac98cd0b322fd9b317b5b0fc61eddf3a3b01091fbeb3d75d9555a6` |
| Upstream revision pin | not yet recorded — capture the HF revision SHA on next re-fetch |
| Staged | `tasks/` (15 task dirs `task_0000NN`), unzipped from the pinned archive |
| License | Apache-2.0 (`LICENSE` in this dir) |
| Sample rule | first 15 task dirs in archive order (`task_000001`–`task_000017`, skipping the two ids the upstream release does not ship: `task_000007`, `task_000015`) — deterministic, no lab-side quality filtering |
| Loader incompatibility | every `task.toml` sets `[task] name = "FACET-Terminal"`, which harbor 0.21.0 rejects (`TaskConfig` requires `org/name`; `TaskModel.is_valid_dir` False; `harbor run -p …` fails "Either datasets or tasks must be provided"). Decision: `research/external/harbor-ecosystem/FACET-LOADER-DECISION.md` — the pack is NOT rewritten |
| Contamination class | synthetic tasks distilled from public Agent Skills via FACET's pipeline; behavior-study only, external-flagged outcomes |


## data-agent-harbor-eval

| Field | Value |
|---|---|
| Source pin | HuggingFace `HuggingEnvs/data-agent-harbor-eval` (144 tasks), built from `jupyter-agent/jupyter-agent-dataset` |
| Upstream revision pin | `f931115192a5dec58102aaf9683454c560b61392` |
| Staged | `tasks/` (144 task dirs), `registry.json`, `manifest.parquet`, `README.md` |
| License | MIT |
| Sample rule | official 144-task validation split |
| Validation observed | built-in `oracle` raises `FileNotFoundError` (upstream pack does not ship `solution/solve.sh`); `nop` scores 0.0 (correctness 0.0, submission 0.0, tool_efficiency 0.0); `cheat` probe scores 0.0 (anti-cheat verifier isolation in Harbor prevents agent access to `/tests` or `EXPECTED_ANSWER`) |
| Contamination class | Kaggle notebooks reverse-engineered into tasks; behavior-study only, external-flagged outcomes |

## wildclawbench-harbor

| Field | Value |
|---|---|
| Source pin | HuggingFace `internlm/WildClawBench-Harbor` (60 tasks), paper arXiv:2605.10912 |
| Upstream revision pin | `33d06066716dc2666721f08ad2784c96e9b44dc5` |
| Staged | 60 task dirs, `README.md` |
| License | MIT |
| Sample rule | full 60-task benchmark suite across 6 categories |
| Validation observed | requires pre-built 13.4GB Docker image `wildclawbench-ubuntu:v1.3` (from `internlm/WildClawBench/Images/wildclawbench-ubuntu_v1.3.tar`); without pre-loaded image, Docker compose fails with pull access denied / RuntimeError |
| Contamination class | long-horizon OS and safety benchmark; behavior-study only, external-flagged outcomes |

## data-eng-bench

| Field | Value |
|---|---|
| Source pin | GitHub `Snowflake-Labs/data-eng-bench` (103 tasks) / Harbor Hub `snowflake-labs/data-eng-bench` |
| Upstream revision pin | git commit `a3278ad102829a6084dde086244a0ef665a8011c` |
| Staged | `tasks/` (30 tasks matching `configs/fast-30.txt`), `fast-30.txt` |
| Base Docker image | `ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0` (tagged as `dbt-bench-base:latest`) |
| License | Apache-2.0 |
| Sample rule | official 30-task `configs/fast-30.txt` fast subset |
| Validation observed | `dbt-fix-division-by-zero` and `dbt-cart-abandonment-recovery` oracle runs completed (0 exceptions); verifier requires `DB_TYPE=duckdb` environment config to avoid Snowflake credentials check |
| Contamination class | dbt enterprise data warehouse benchmarks; behavior-study only, external-flagged outcomes |
## Regeneration

Commands to re-derive both packs live with the acquisition notes in
`research/external/harbor-ecosystem/`. Re-fetch must verify the archive digest
above before replacing anything under `tasks/`.
