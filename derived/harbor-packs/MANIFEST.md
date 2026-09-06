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

## Regeneration

Commands to re-derive both packs live with the acquisition notes in
`research/external/harbor-ecosystem/`. Re-fetch must verify the archive digest
above before replacing anything under `tasks/`.
