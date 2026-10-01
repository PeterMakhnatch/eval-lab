---
status: living
audience:
  - builder
  - analyst
  - operator
---

# HAR-136 source inventory and retirement decisions

Complete **240-module** snapshot at commit `dd5ff4d77e548fd5afcf40fbe5ff149d4e1fa92b` (2026-10-01), starting from [the generated repository map](repo-map.md). Physical source size: **194,118 lines**, including blanks/comments and every package initializer. This is a pinned audit, not an assertion that later main has the same size.

## Method and evidence limits

- Parse the tracked Python import graph, including function-local imports, package-relative imports/re-exports, and implicit ancestor-package initialization. Separate TYPE_CHECKING imports from runtime edges.
- Roots: registered CLI handlers, runner/process_job, real module-main guards, external scripts, and qualified dynamic/configuration or pinned-artifact contracts. Constructing an ArgumentParser alone is not an entrypoint. Scan tracked YAML/JSON/TOML/shell/SQL/Markdown as well as Python; distinguish invocation/configuration from narrative mentions. Bare filenames are not qualified references.
- The conservative closure reaches **224 modules** from operational or declared config/contract roots; **16** have no such path. This is potential reachability, **not execution coverage or proof of production readiness**. Contract-bound code may have no Python caller and still require retention. All **11 package initializers** are reached. Protection is independent of reachability.
- Parent replay reproduced every inventory record and verified all 240 source hashes against the tree, joined with bulk Git history/LOC measurements. One-off audit script SHA256: `a42969e3c0b8c3c27a191a257a5a01f7a66306fea2db1b118404332463db40f5`. No new runtime audit framework was installed.
- Last-commit timestamps are UTC. Card references are literal HAR IDs in the last commit message or first 30 source lines; **139 modules have no such ID**. A reference is not proof of current ownership. The permanent source lane is Platform under agents/OWNERS.md; current mission leases remain on the task board. Unknown card provenance is recorded, not invented.
- Closed-world limit: repository-visible callers/configuration are covered; arbitrary external programs and computed import strings cannot be exhaustively proved absent. Protected or uncertain modules are not deleted merely because the static graph misses them.

## Decisions and narrow changes

The intended deletion lists were posted on HAR-130 **before edits**. The three flat orphan modules have no tracked runtime/configuration consumer; their only code callers are their own tests:

| Module | Source LOC | Own collected cases | Disposition |
|---|---:|---:|---|
| `loader_shims.py` | 38 | 4 | Retire standalone unwired prototypes and own tests; retain the documented value-choice policies as recommendations, not integrated compatibility. |
| `dsh.py` | 520 | 14 | Separate small retirement PR for reader, own tests and sole native-session fixture; preserve ATIF intake/readiness and qualify obsolete comparison comments. |
| `trajectory_action_taxonomy.py` | 560 | 5 | Separate small retirement PR for unused classifier/own tests; correct historical capability claims, retain the active error taxonomy and schema alphabet fields. |

**False-dead candidates rejected:** `mini_observation_masking.py` is selected by `research/analysis/harness-first/observation-masking.yaml`; `upstream_adapter.py` is bound by adapter manifests/fixture hashes. They stay. Both TrajectoryIR implementations and tonight's runner/capture/counts/ledger/serving/SFT path stay untouched.

Two exact duplicate implementations are consolidated into existing storage authorities:

- `analyst._resolve_runs_roots` → `storage.paths.resolve_runs_roots`; migrate its caller and remove the duplicate/import, preserving explicit/env/discovery ordering, symlink deduplication and the existing empty fallback.
- Both PostgreSQL target-label copies → `storage.attach.postgres_identity`; migrate attach plus both verdict catalog readers, without keeping the old private-name alias. User/password fields remain outside the display label.

Other candidates deliberately **not** consolidated: the statistics/capability digest cycle; leaf timestamp readers that would gain a back-dependency on the large run-report module; queue/preflight formatting on the live operator path; self-file adapter digests; provider-specific credential guards; domain-plugin boundaries; and schema-specific Parquet writers. Text equality alone does not justify those dependencies or contract changes. Four unreached interpretation modules (alignment, context, semantic producers, sequence) remain explicit holds rather than speculative deletions.

## Measured parent cut and verification

This parent change alone (loader prototypes plus the two duplicate cutovers) changes source **240 → 239 modules**, **194,118 → 194041 physical lines**: **77 fewer source lines**, and removes **4 collected tests**. The two independent retirement PRs are measured separately; their delivery and the combined current-main receipt belong to HAR-136, not an inferred total in this baseline table.

- Before/after CPU probes preserve four path-selection boundaries and four DSN formatting cases. Actual `resolve_trial` consumes a temporary raw trial through the surviving path resolver; actual verdict catalog readers refuse malformed DSNs with exit 2 and the expected target label. The unavailable external catalog/SQL side effect is injected in the local intake/attach probe; no live database, model, task or cloud execution is claimed.
- Focused consumer checks: **53 passed, 2 skipped** (paths, analyst, verdicts and four Parquet-attach cases), with external Z2 fixed unavailable and socket connections forbidden. Full matrices remain CI's job. Existing semantic_facts construct-shadowing warnings were observed, not suppressed.
- Baseline hosted CI at dd5ff4d7: quality run 36818564034 **4m14s**, typecheck 36818563874 **1m37s**, profile workflow 36818563911 **1m49s**, wheelhouse 36818563895 **54s**. Each Python version collected **5,902 cases** across its two shards (5,772 passed, 129 skipped, 1 xfailed). Later CI times are observations, not causal speedup claims; concurrent source work and runner noise must be separated from this cut's measured delta.

## Complete baseline module table

`spine` means a transitive path from runner/process_job; root categories overlap. `tests only` means no operational/config root was found, not automatic deletion approval. The final column is **card evidence**, not an invented ownership assignment. Source paths below are relative to src/evallab at the pinned baseline, including modules selected for retirement.

| Module | Physical LOC | Potential roots | Decision | Last source commit UTC | Last source commit | Card evidence |
|---|---:|---|---|---|---|---|
| `__init__.py` | 3 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-08-14T17:50:08Z | `3e2dd7b5255e06e060ea235bbe6eecd2ba34d69c` | not recorded |
| `agentabstain_gate.py` | 834 | external | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `analysis_capability.py` | 2074 | CLI, module CLI, config/contract | keep | 2026-08-30T23:58:16Z | `0192870b345829deb92cf1c811f082f5fabce1d2` | not recorded |
| `analysis_statistics.py` | 814 | CLI, module CLI, config/contract | keep | 2026-08-30T17:50:51Z | `3ec6b7a296fba5ba8377f53d5de2af1c58496716` | not recorded |
| `analysis_worker.py` | 1124 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `analyst.py` | 1704 | CLI, module CLI, config/contract | keep | 2026-08-31T00:42:47Z | `b480e643e0e6f10de468e718cc880155151889fc` | not recorded |
| `antigravity.py` | 335 | config/contract | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `authoring.py` | 3616 | module CLI | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `automation.py` | 1168 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `backups.py` | 173 | CLI, module CLI, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `behavior_calibration.py` | 274 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-25T00:21:34Z | `fba2e1357afdade17f365464a5aa50ab6a67631d` | not recorded |
| `behavior_catalog.py` | 155 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-25T00:21:34Z | `fba2e1357afdade17f365464a5aa50ab6a67631d` | not recorded |
| `behavior_episodes.py` | 847 | tests only | keep: protected | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `behavior.py` | 736 | CLI, module CLI, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `benchmark_program_contracts.py` | 233 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `calibrate.py` | 1967 | CLI, module CLI, external, config/contract | keep | 2026-09-18T02:56:35Z | `4d95c0429b8541a4eada72be2b4b0c8f12b65fe7` | HAR-60 |
| `campaigns.py` | 2702 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-29T23:30:40Z | `77df1d4f45a14814c619b615d7758b3353a7177b` | HAR-104 |
| `canary.py` | 217 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-06T18:56:50Z | `b9fca536dbd673543959b6c32bdca36b364410cd` | not recorded |
| `capability_contract.py` | 910 | CLI, module CLI, config/contract | keep | 2026-08-27T06:01:13Z | `d68c1fb735b40a488014abc8c843393e87d2c215` | not recorded |
| `cards.py` | 649 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `cli.py` | 6067 | CLI, module CLI, config/contract | keep | 2026-10-01T04:58:33Z | `872fd997c4d05409c699284c49648381f333f14e` | HAR-126 |
| `cohort.py` | 2420 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-29T04:09:33Z | `a6a780267f45e57a9501f3ccbd0b2fdd7da57511` | HAR-90 |
| `contextpack.py` | 1343 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `continuous_control_plane.py` | 191 | module CLI, config/contract | keep | 2026-08-29T05:55:04Z | `6bd08acb29fe5546c9edfec337365fa6053e828e` | not recorded |
| `counts.py` | 431 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T04:25:00Z | `e9cd895ff94f292affbd6924956ed16947da12f3` | HAR-100, HAR-113, HAR-115, HAR-78 |
| `craft.py` | 2112 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-24T23:13:09Z | `7bacbff73fe2102b5538e84efbca73a58d384404` | not recorded |
| `credentials.py` | 352 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-29T23:30:40Z | `77df1d4f45a14814c619b615d7758b3353a7177b` | HAR-104 |
| `curve.py` | 608 | CLI, module CLI, config/contract | keep | 2026-09-14T21:29:03Z | `7c220b91671aa5aa25ae81b3ee78605c4fce6574` | not recorded |
| `database.py` | 682 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-29T23:18:07Z | `514ba610582404ddcb28dc919106eccdea74218f` | HAR-104 |
| `deepplanning.py` | 400 | external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `devloop.py` | 353 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `digest.py` | 864 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-18T02:56:35Z | `4d95c0429b8541a4eada72be2b4b0c8f12b65fe7` | HAR-60 |
| `docindex.py` | 405 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `dsh.py` | 520 | tests only | retire | 2026-09-16T21:34:37Z | `f405468f8929d9e5afed4afab9b518a2e0962eab` | not recorded |
| `edit_signals.py` | 41 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T01:29:51Z | `c9bcb6ce4274acfedc23b097dd6704c6ee1e1a15` | HAR-116 |
| `eventlog.py` | 50 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-15T05:51:37Z | `fefabe3839b111e85f3ef4897933fcaaf2b13536` | not recorded |
| `evidence_store.py` | 561 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-29T05:55:04Z | `6bd08acb29fe5546c9edfec337365fa6053e828e` | not recorded |
| `evidence/__init__.py` | 29 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-08-30T18:10:09Z | `663cfdf8924c3cee4762408980a34e9854a4a6c8` | not recorded |
| `evidence/atif.py` | 1173 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `evidence/capture_authority.py` | 773 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-30T18:10:09Z | `663cfdf8924c3cee4762408980a34e9854a4a6c8` | not recorded |
| `evidence/event_mart.py` | 505 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `evidence/facts.py` | 1932 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `evidence/llm_request.py` | 397 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-02T22:05:55Z | `1e95553fdc80642d0784f9d573962fd49bba2928` | not recorded |
| `evidence/parquet_io.py` | 336 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T23:26:12Z | `57dd709111401539f861fc61a7d3df6bd828c0e6` | not recorded |
| `evidence/reef_intake.py` | 1308 | module CLI | keep | 2026-09-25T06:45:27Z | `5bc80c1bca33bc7af5a1dba00a299a60928145e8` | not recorded |
| `evidence/reef_shift.py` | 289 | module CLI | keep | 2026-09-25T07:04:19Z | `82fb69d24142ae5efc925c9d83c0d266ce97831e` | not recorded |
| `execution_contracts.py` | 1920 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T04:46:24Z | `0e2a3eb09145fbad09ece4a750d84d9aaad6f5c0` | HAR-129 |
| `explorer.py` | 2503 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `fetch.py` | 1055 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-27T18:44:34Z | `29214980523d2d062b660d6ffe22a16c58fb6df2` | not recorded |
| `gc.py` | 734 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-02T20:45:24Z | `b4b88f1917111630e541b7180fba5f8564df90ac` | not recorded |
| `gepa_optimizer/__init__.py` | 1 | CLI, module CLI, config/contract | keep: protected | 2026-09-14T22:35:24Z | `aaf049b2b3f35d3e2ebfb76a531be83cb13f780b` | HAR-23, HAR-38, HAR-48, HAR-5 |
| `gepa_optimizer/__main__.py` | 195 | module CLI | keep | 2026-09-30T05:19:04Z | `6311ac155fb57bf962dd156fb9b035daba514cb7` | HAR-110 |
| `gepa_optimizer/budget.py` | 269 | module CLI | keep | 2026-09-30T22:55:40Z | `79586a9b6f5a92dd3f540226f9b364a31d97217e` | not recorded |
| `gepa_optimizer/codex_transport.py` | 481 | module CLI | keep | 2026-09-30T22:55:40Z | `79586a9b6f5a92dd3f540226f9b364a31d97217e` | not recorded |
| `gepa_optimizer/composition.py` | 360 | module CLI | keep | 2026-09-16T15:30:40Z | `eafb84187ba3e2c557b609fe48fc3fd34db5368b` | HAR-54 |
| `gepa_optimizer/evaluator.py` | 1386 | module CLI | keep | 2026-09-30T06:22:34Z | `3235f360a866770ec34764235b4e75a26c455cb5` | HAR-110 |
| `gepa_optimizer/feedback.py` | 1103 | module CLI | keep | 2026-09-21T01:33:00Z | `decc3c95802fef8ecc42d76588058c2b8912ae14` | not recorded |
| `gepa_optimizer/intake.py` | 167 | CLI, module CLI, config/contract | keep | 2026-09-28T22:16:24Z | `9329fa9d767c224a1c1ae710e9a8a6b2e1082a9b` | HAR-81, HAR-82, HAR-83 |
| `gepa_optimizer/meta_engine.py` | 563 | module CLI | keep | 2026-09-18T02:38:09Z | `df4ac8925eee6db676696b762d6af7aceba29c36` | HAR-59 |
| `gepa_optimizer/opencode_transport.py` | 430 | module CLI | keep | 2026-09-18T02:38:09Z | `df4ac8925eee6db676696b762d6af7aceba29c36` | HAR-59 |
| `gepa_optimizer/paired_analysis.py` | 632 | module CLI | keep | 2026-09-14T22:35:24Z | `aaf049b2b3f35d3e2ebfb76a531be83cb13f780b` | HAR-23, HAR-38, HAR-48, HAR-5 |
| `gepa_optimizer/proposer.py` | 267 | module CLI | keep | 2026-09-30T22:55:40Z | `79586a9b6f5a92dd3f540226f9b364a31d97217e` | not recorded |
| `gepa_optimizer/release.py` | 26 | module CLI | keep | 2026-09-14T22:35:24Z | `aaf049b2b3f35d3e2ebfb76a531be83cb13f780b` | HAR-23, HAR-38, HAR-48, HAR-5 |
| `gepa_optimizer/workflow.py` | 1074 | module CLI | keep | 2026-09-30T22:55:40Z | `79586a9b6f5a92dd3f540226f9b364a31d97217e` | not recorded |
| `governance.py` | 254 | module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `harbor_antigravity.py` | 161 | config/contract | keep: protected | 2026-08-20T01:12:34Z | `c4eac8edefe797841f6745c999f0fea6dcfcd164` | not recorded |
| `harbor_codex.py` | 28 | config/contract | keep: protected | 2026-08-23T16:35:41Z | `217831110b3cf768a41860bff1b8a731b17ee8a6` | not recorded |
| `harbor_common.py` | 78 | CLI, module CLI, external, config/contract | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `harbor_daytona.py` | 318 | config/contract | keep: protected | 2026-10-01T00:11:33Z | `93df9bd1ac34f1e1444a0bcc1991dc1aa0b21dc7` | HAR-122 |
| `harbor_deepseek.py` | 116 | config/contract | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `harbor_glm_selfhosted.py` | 170 | config/contract | keep: protected | 2026-09-18T17:35:31Z | `a955c783693641f5a75fcb0215d280cd24dc41c2` | HAR-63 |
| `harbor_network.py` | 318 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-08-28T03:56:09Z | `0207b98a13d42e4a490fcf7af2aaf480e9287b27` | not recorded |
| `harbor_repeat_verifier.py` | 336 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-28T18:53:23Z | `af41292645e6dbdfb23056a5a5cef71de8b322d0` | HAR-81, HAR-83 |
| `harbor_rlm.py` | 249 | external, config/contract | keep: protected | 2026-09-28T19:54:23Z | `0e99331f45c1bfbdb8f0dc4b8db999fc83aa5723` | HAR-85 |
| `harbor_state_journal.py` | 291 | config/contract | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `harbor_terminus.py` | 1135 | CLI, module CLI, external, config/contract | keep: protected | 2026-10-01T00:49:34Z | `2e042ad63bcdb1a361a6edfe394a480f83f22aa2` | HAR-114, HAR-116, HAR-96 |
| `harbor_zai_miniswe.py` | 150 | config/contract | keep: protected | 2026-09-21T21:06:44Z | `e7a38787d81a2b625039b00e7d52db6ebb9b79bf` | not recorded |
| `harbor_zai_opencode.py` | 293 | module CLI, config/contract | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `hidden_patch.py` | 165 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T07:32:43Z | `e8304c199ce0ea5826204b87dc58cd933d9d5533` | HAR-108 |
| `host_task_staging.py` | 606 | external | keep | 2026-09-18T03:33:09Z | `e36bfedf561cf1f6fcdc8886fed478937288fa50` | HAR-62 |
| `ingest_verify.py` | 482 | module CLI | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `inspect_adapter.py` | 1066 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-31T11:17:52Z | `58c9b5924a2ff0f79f0206ef93aaf794924225c8` | not recorded |
| `interpretation/__init__.py` | 1 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/benchmark_events.py` | 1270 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/benchmark_projection.py` | 310 | CLI, module CLI, config/contract | keep | 2026-08-29T01:42:00Z | `3151ef30ad863d0bb10cc7f6edad0f3877a7985a` | not recorded |
| `interpretation/c2_intervention_gate.py` | 552 | module CLI | keep | 2026-08-29T23:06:32Z | `10fbccf612620b79eaf53cd3162171c48d2ced00` | not recorded |
| `interpretation/claude_sessions.py` | 150 | CLI, module CLI, external, config/contract | keep | 2026-09-26T04:15:12Z | `de178e8c3d3145bb84e4b339381ee86f0c85c5f3` | HAR-76 |
| `interpretation/codex_rollouts.py` | 513 | CLI, module CLI, external, config/contract | keep | 2026-09-26T03:49:35Z | `2a09094daf37e323b68102f3ea0b96b1588c68cd` | HAR-76 |
| `interpretation/domains/__init__.py` | 102 | CLI, module CLI, external, config/contract | keep: protected | 2026-09-26T04:10:12Z | `077aa330a9f7bed02558e64e91107188f232070b` | HAR-76 |
| `interpretation/domains/atlas_finance.py` | 247 | CLI, module CLI, external, config/contract | keep | 2026-09-26T03:25:10Z | `c183b17d551b90db1381919b5701e8617fcdae8b` | HAR-76 |
| `interpretation/domains/ceo_bench.py` | 196 | CLI, module CLI, external, config/contract | keep | 2026-09-26T04:20:21Z | `564a49e5e19d04563efdd84bcd1ca61b272aff83` | HAR-76 |
| `interpretation/domains/synthetic_hospital.py` | 335 | CLI, module CLI, external, config/contract | keep | 2026-09-26T03:34:10Z | `2dd6faa62aff6b12a085b9a628fc906de780399a` | HAR-76 |
| `interpretation/evidence_pack.py` | 896 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/feature_registry.py` | 3048 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/outside_fetch.py` | 579 | CLI, module CLI, external, config/contract | keep | 2026-09-30T06:55:09Z | `0105e6ff9ef953aa985dea9176f183028cf9b245` | HAR-109, HAR-111, HAR-81 |
| `interpretation/price_table.py` | 168 | CLI, module CLI, external, config/contract | keep | 2026-09-26T03:16:31Z | `5fac0deef60f5d1b317e0a209813cfee6d5e9cb5` | HAR-76 |
| `interpretation/producers/__init__.py` | 79 | CLI, module CLI, config/contract | keep: protected | 2026-09-02T21:35:54Z | `4200a99e329636354df747f89c3126dd8577f132` | not recorded |
| `interpretation/producers/action_memory.py` | 736 | CLI, module CLI, config/contract | keep | 2026-08-30T23:58:16Z | `0192870b345829deb92cf1c811f082f5fabce1d2` | not recorded |
| `interpretation/producers/mcp_funcdag.py` | 381 | CLI, module CLI, config/contract | keep | 2026-08-30T23:58:16Z | `0192870b345829deb92cf1c811f082f5fabce1d2` | not recorded |
| `interpretation/producers/mcp_recovery.py` | 406 | CLI, module CLI, config/contract | keep | 2026-08-30T23:58:16Z | `0192870b345829deb92cf1c811f082f5fabce1d2` | not recorded |
| `interpretation/producers/memory_continuity.py` | 392 | CLI, module CLI, config/contract | keep | 2026-09-02T21:35:54Z | `4200a99e329636354df747f89c3126dd8577f132` | not recorded |
| `interpretation/run_report_scale.py` | 261 | CLI, module CLI, external, config/contract | keep | 2026-09-26T04:04:15Z | `c4493126d379f78ab4b84b58700c21a6f5a62485` | HAR-76 |
| `interpretation/run_report.py` | 3436 | CLI, module CLI, external, config/contract | keep | 2026-09-30T06:55:09Z | `0105e6ff9ef953aa985dea9176f183028cf9b245` | HAR-109, HAR-111, HAR-81 |
| `interpretation/trace_readiness.py` | 311 | module CLI | keep | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `interpretation/traj_baseline.py` | 1109 | CLI, module CLI, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `interpretation/traj_card.py` | 821 | CLI, module CLI, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `interpretation/trajectory_acceptance.py` | 280 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_alignment.py` | 429 | tests only | hold: unresolved | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_calibration.py` | 296 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_compliance_ops.py` | 318 | CLI, module CLI, config/contract | keep | 2026-08-28T23:53:04Z | `759196c9827782dbbea2bb29ad508121cb8dcdec` | not recorded |
| `interpretation/trajectory_compliance.py` | 538 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-28T23:53:04Z | `759196c9827782dbbea2bb29ad508121cb8dcdec` | not recorded |
| `interpretation/trajectory_context.py` | 1282 | tests only | hold: unresolved | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/trajectory_data_quality.py` | 1438 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_hydration.py` | 724 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-29T13:58:32Z | `13ceb0706798bb4f621e4e3ecd790df1558f6e46` | HAR-81 |
| `interpretation/trajectory_ir.py` | 1693 | CLI, module CLI, config/contract | keep: protected | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `interpretation/trajectory_judgment.py` | 177 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_quality.py` | 620 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-15T22:49:25Z | `8ae1ae37693386df3812cb159a7a12716b70a322` | not recorded |
| `interpretation/trajectory_readiness.py` | 277 | module CLI | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_recipe_run.py` | 654 | module CLI | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/trajectory_recipes.py` | 1945 | module CLI | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/trajectory_runtime.py` | 2935 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/trajectory_semantic_producers.py` | 459 | tests only | hold: unresolved | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `interpretation/trajectory_semantics.py` | 1588 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `interpretation/trajectory_sequence.py` | 848 | tests only | hold: unresolved | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `labels.py` | 857 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `ladder.py` | 1515 | CLI, module CLI, config/contract | keep | 2026-08-24T02:26:45Z | `1c220c63454ceb657d71f33a60726636dfc30909` | not recorded |
| `lance.py` | 1606 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `ledger.py` | 254 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-29T23:18:07Z | `514ba610582404ddcb28dc919106eccdea74218f` | HAR-104 |
| `lego_capture.py` | 435 | module CLI | keep | 2026-09-14T21:29:03Z | `7c220b91671aa5aa25ae81b3ee78605c4fce6574` | not recorded |
| `lessons.py` | 1314 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `lineage.py` | 572 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `loader_shims.py` | 38 | tests only | retire | 2026-09-07T01:55:27Z | `76a5f2520246998d5bbcfd50f2aa8828e7a6c098` | not recorded |
| `loopfix.py` | 234 | CLI, module CLI, external, config/contract | keep: protected | 2026-09-30T22:29:29Z | `7eed6a5bf3532361856a7923b529b267e694417d` | HAR-114, HAR-116 |
| `mcp_substrate.py` | 1870 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `mimo_exploit.py` | 437 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T01:29:51Z | `c9bcb6ce4274acfedc23b097dd6704c6ee1e1a15` | HAR-116 |
| `mimo_tool_calls.py` | 628 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T00:49:34Z | `2e042ad63bcdb1a361a6edfe394a480f83f22aa2` | HAR-114, HAR-116, HAR-96 |
| `mini_observation_masking.py` | 41 | config/contract | keep: protected | 2026-09-15T22:57:10Z | `04d0449989695a36ebae0c8eb540ccbf8d6004ad` | HAR-50 |
| `modal_billing.py` | 212 | CLI, module CLI, config/contract | keep | 2026-10-01T04:46:24Z | `0e2a3eb09145fbad09ece4a750d84d9aaad6f5c0` | HAR-129 |
| `modal_ops.py` | 411 | CLI, module CLI, config/contract | keep | 2026-10-01T04:46:24Z | `0e2a3eb09145fbad09ece4a750d84d9aaad6f5c0` | HAR-129 |
| `model_capture.py` | 1728 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-28T19:22:40Z | `c8b630b78328a2d071ca47d0b5b85ffa31a57810` | HAR-81, HAR-82 |
| `modeladapter.py` | 388 | module CLI | keep: protected | 2026-08-31T00:42:47Z | `b480e643e0e6f10de468e718cc880155151889fc` | not recorded |
| `observation_masking.py` | 249 | module CLI, config/contract | keep | 2026-09-15T22:57:10Z | `04d0449989695a36ebae0c8eb540ccbf8d6004ad` | HAR-50 |
| `operational_restraint.py` | 2006 | tests only | keep: protected | 2026-08-26T07:09:49Z | `c37b7c7b61b013482fa20a70a0e06e5c945513e6` | not recorded |
| `ops_continuous.py` | 2349 | module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `phoenix_annotations.py` | 464 | tests only | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `power.py` | 358 | CLI, module CLI, external, config/contract | keep | 2026-09-25T06:16:47Z | `ef4d587bcd20c5517f919c57b1268f5fcbd3953e` | HAR-71, HAR-72, HAR-73 |
| `preflight.py` | 646 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-02T20:45:24Z | `b4b88f1917111630e541b7180fba5f8564df90ac` | not recorded |
| `probe03.py` | 3496 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `process_job.py` | 951 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T04:25:00Z | `e9cd895ff94f292affbd6924956ed16947da12f3` | HAR-107 |
| `profiles.py` | 1112 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T02:48:54Z | `f1bce0f9f176e654b61384269d83ded1be35a3cb` | HAR-104 |
| `provenance.py` | 393 | module CLI | keep | 2026-08-29T22:01:13Z | `83c60167c2aca97e1375d148fb33ec8442043fba` | not recorded |
| `quality_audit.py` | 288 | CLI, module CLI, config/contract | keep | 2026-09-14T21:29:03Z | `7c220b91671aa5aa25ae81b3ee78605c4fce6574` | HAR-25 |
| `queue.py` | 2688 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-30T04:11:49Z | `81d764036c3c3b4c45b7989f4f1e5de9089afe07` | HAR-107 |
| `quota.py` | 1011 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-30T02:48:54Z | `f1bce0f9f176e654b61384269d83ded1be35a3cb` | HAR-104 |
| `recovery/__init__.py` | 51 | config/contract | keep: protected | 2026-09-02T20:45:24Z | `b4b88f1917111630e541b7180fba5f8564df90ac` | not recorded |
| `recovery/bundle.py` | 216 | config/contract | keep | 2026-08-25T06:17:39Z | `4a8e5cfe8f11c0d81c9c5f3a0248c683637ab9e5` | not recorded |
| `recovery/certify.py` | 260 | config/contract | keep | 2026-08-25T05:20:59Z | `c57bb577c928e49f416ea8829fe0fb4c28a86280` | not recorded |
| `recovery/wrapper.py` | 53 | config/contract | keep | 2026-09-02T20:45:24Z | `b4b88f1917111630e541b7180fba5f8564df90ac` | not recorded |
| `registry.py` | 2068 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-18T03:33:09Z | `e36bfedf561cf1f6fcdc8886fed478937288fa50` | HAR-62 |
| `regrade.py` | 617 | CLI, module CLI, config/contract | keep | 2026-09-07T02:14:45Z | `000750a897b612948067e3d00651f56da501bcbd` | not recorded |
| `repetition.py` | 596 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-18T17:35:31Z | `a955c783693641f5a75fcb0215d280cd24dc41c2` | HAR-63 |
| `repomap.py` | 1147 | module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `report.py` | 519 | CLI, module CLI, config/contract | keep | 2026-08-27T18:44:34Z | `29214980523d2d062b660d6ffe22a16c58fb6df2` | not recorded |
| `researchers.py` | 1441 | CLI, module CLI, config/contract | keep | 2026-09-06T18:36:41Z | `97372a240f79b9c1ca0e42415d418cc027af14d1` | not recorded |
| `restraint_canary.py` | 781 | module CLI | keep | 2026-08-28T04:09:14Z | `c2dd3c8973937077df936f65c90275b05acbe3ef` | not recorded |
| `results_home.py` | 872 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T01:37:20Z | `a87e01afed2115a54f741f8daf723166659d40a1` | HAR-117 |
| `results.py` | 325 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-29T19:08:52Z | `752307a00bf56f4c2e0547fded55182f135d4076` | not recorded |
| `rlm/__init__.py` | 1 | module CLI, external, config/contract | keep: protected | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench_report.py` | 195 | module CLI | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench_runner.py` | 278 | module CLI | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench/__init__.py` | 6 | module CLI, config/contract | keep: protected | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench/generators.py` | 449 | module CLI, config/contract | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench/memo_family.py` | 231 | module CLI, config/contract | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/bench/scoring.py` | 49 | module CLI, config/contract | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/gepa_rlm.py` | 189 | module CLI | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/harness.py` | 493 | module CLI, external, config/contract | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/policies.py` | 321 | module CLI, external, config/contract | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `rlm/traj_report.py` | 237 | module CLI | keep | 2026-09-16T20:08:54Z | `fa29e73a0cb4dd79104d1ba1a8635bd0814a9d99` | HAR-55 |
| `roster.py` | 129 | module CLI | keep | 2026-09-16T21:04:08Z | `933511219cbf7ca314046f5a6f83dbd3b4234b61` | not recorded |
| `run_preflight.py` | 724 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `run_telemetry.py` | 579 | CLI, module CLI, external, config/contract | keep | 2026-10-01T04:58:33Z | `872fd997c4d05409c699284c49648381f333f14e` | HAR-126 |
| `runner.py` | 2727 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T04:46:24Z | `0e2a3eb09145fbad09ece4a750d84d9aaad6f5c0` | HAR-129 |
| `schemas/__init__.py` | 2522 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-28T21:33:52Z | `dcf5af4f73c690892ebcf0373eb41fae0371ead8` | not recorded |
| `screen.py` | 1131 | CLI, module CLI, config/contract | keep | 2026-08-24T07:32:26Z | `7a02fde85ed11bbaeec89928e72fe03d622e5d60` | not recorded |
| `semantic_facts.py` | 481 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `seqgen.py` | 1462 | module CLI | keep | 2026-08-24T06:01:23Z | `45516f89a00da9d25a785737be2c7aa59b11b7c3` | not recorded |
| `sft_glm.py` | 1412 | module CLI | keep: protected | 2026-09-21T19:56:52Z | `8dcae24ba3be6b89a21f5526f3ad63bf85365d2a` | HAR-64, HAR-65, HAR-66 |
| `sft_records.py` | 1040 | module CLI | keep: protected | 2026-09-29T13:58:32Z | `13ceb0706798bb4f621e4e3ecd790df1558f6e46` | HAR-81 |
| `sft_split.py` | 410 | module CLI | keep: protected | 2026-09-28T22:21:21Z | `3da42d717e153d59a33172fa1ed0fccf5b2b8f65` | HAR-81, HAR-84 |
| `sft_terminus.py` | 1054 | module CLI | keep: protected | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `sft_tinker.py` | 599 | module CLI | keep: protected | 2026-09-28T22:21:21Z | `3da42d717e153d59a33172fa1ed0fccf5b2b8f65` | HAR-81, HAR-84 |
| `smoke.py` | 358 | module CLI, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `spend_day.py` | 777 | CLI, module CLI, config/contract | keep: protected | 2026-10-01T00:19:58Z | `3f5c7ce483fbb91fd552f712448afb4566cd32ab` | HAR-122 |
| `spine.py` | 168 | module CLI | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `state_events.py` | 660 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-27T06:36:27Z | `0f90ab3c396524f31a7e12c0f7b997992904c307` | not recorded |
| `status_generator.py` | 683 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `status.py` | 538 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `step_layers.py` | 1134 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T05:02:03Z | `48d4b6eddb5c5c368b95deb4d99737f47d304a52` | HAR-107, HAR-110 |
| `storage/__init__.py` | 1 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-08-27T18:59:40Z | `d201d2e5157a790068bd8ca8aec9f432aa0da013` | not recorded |
| `storage/attach.py` | 679 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T23:26:12Z | `57dd709111401539f861fc61a7d3df6bd828c0e6` | not recorded |
| `storage/data_backfill.py` | 672 | CLI, module CLI, config/contract | keep | 2026-08-27T19:10:32Z | `5c7079005754862195f4e98cf7f4f135a193e34d` | not recorded |
| `storage/fs.py` | 45 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `storage/inspect_storage.py` | 250 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-31T11:17:52Z | `58c9b5924a2ff0f79f0206ef93aaf794924225c8` | not recorded |
| `storage/parquet_compaction.py` | 1057 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `storage/paths.py` | 363 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `storm.py` | 517 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-17T21:22:27Z | `d8b393ec90cb86a89dff3ffa2b2f516db85ef713` | not recorded |
| `synthetic_contracts.py` | 351 | tests only | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `synthetic_funcdag.py` | 1273 | tests only | keep: protected | 2026-08-28T03:56:09Z | `0207b98a13d42e4a490fcf7af2aaf480e9287b27` | not recorded |
| `task_candidate.py` | 329 | tests only | keep: protected | 2026-09-28T18:31:14Z | `60e9c6b5907a83b1cc7282f6a9ed18581e193732` | HAR-67, HAR-82 |
| `task_catalog.py` | 1747 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-28T21:55:56Z | `e456c9f84b314e6b99ae5ccc0051b397ce986f73` | not recorded |
| `task_health.py` | 1489 | CLI, module CLI, external, config/contract | keep: protected | 2026-09-30T23:32:59Z | `71f863461b91150071ccfb65433a0adfe5d8bf58` | HAR-113 |
| `task_import.py` | 308 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-22T23:02:12Z | `8881366e8eb5141bee715bb03514600e237bf9e4` | not recorded |
| `task_lint.py` | 636 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-28T19:49:48Z | `d0da6b3d986b28d9dee8d6681e79b56fa3931467` | HAR-82, HAR-83 |
| `task_prepare.py` | 575 | CLI, module CLI, config/contract | keep | 2026-09-29T23:30:40Z | `77df1d4f45a14814c619b615d7758b3353a7177b` | HAR-104 |
| `task_qualification.py` | 1067 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T07:32:43Z | `e8304c199ce0ea5826204b87dc58cd933d9d5533` | HAR-108 |
| `task_stability.py` | 432 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-28T18:53:23Z | `af41292645e6dbdfb23056a5a5cef71de8b322d0` | HAR-81, HAR-83 |
| `task_variants.py` | 1036 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-30T11:15:47Z | `b62d41d6ea5e43bbc163e28ebde41bddef518c7f` | not recorded |
| `task_workbench.py` | 8543 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-18T03:33:09Z | `e36bfedf561cf1f6fcdc8886fed478937288fa50` | HAR-62 |
| `terminus_harness.py` | 463 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T00:49:34Z | `2e042ad63bcdb1a361a6edfe394a480f83f22aa2` | HAR-114, HAR-116, HAR-96 |
| `terminus_local.py` | 116 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-25T02:09:30Z | `dde797ac5377cecf24e4e1e2216207de61ce9787` | not recorded |
| `tidy.py` | 1368 | CLI, module CLI, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `token_flow.py` | 975 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T01:29:51Z | `c9bcb6ce4274acfedc23b097dd6704c6ee1e1a15` | HAR-116 |
| `toolbox.py` | 214 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-16T15:30:40Z | `eafb84187ba3e2c557b609fe48fc3fd34db5368b` | HAR-54 |
| `tracing.py` | 804 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `training_eligibility.py` | 189 | module CLI | keep: protected | 2026-09-14T21:29:03Z | `7c220b91671aa5aa25ae81b3ee78605c4fce6574` | not recorded |
| `training_pool.py` | 338 | module CLI | keep: protected | 2026-09-14T21:29:03Z | `7c220b91671aa5aa25ae81b3ee78605c4fce6574` | not recorded |
| `traj.py` | 2964 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T01:29:51Z | `c9bcb6ce4274acfedc23b097dd6704c6ee1e1a15` | HAR-116 |
| `trajectory_action_taxonomy.py` | 560 | tests only | retire | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `trajectory_error_taxonomy.py` | 308 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T00:16:24Z | `5ebb8248146a284d94f92d48721bb217ef3b72b2` | HAR-106 |
| `trajectory_ir.py` | 781 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-09-25T20:01:25Z | `a320f0a9a27bd86c64976399ec72bca36257e35d` | HAR-75 |
| `trajectory_loss_manifest.py` | 849 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-28T03:29:00Z | `c00b48e9981d41ed25e94b48957202c1a0207e41` | not recorded |
| `trial_decision.py` | 527 | CLI, spine, module CLI, external, config/contract | keep: protected | 2026-10-01T00:15:35Z | `453cbade07e6647b514c84b0c488a679d32d048b` | HAR-109, HAR-121, HAR-78, HAR-96 |
| `trial_diagnosis.py` | 1317 | CLI, spine, module CLI, external, config/contract | keep | 2026-10-01T01:29:51Z | `c9bcb6ce4274acfedc23b097dd6704c6ee1e1a15` | HAR-116 |
| `trial_treatment.py` | 1317 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T23:26:12Z | `57dd709111401539f861fc61a7d3df6bd828c0e6` | HAR-93 |
| `upstream_adapter.py` | 567 | config/contract | keep | 2026-08-24T05:46:54Z | `dade16388df78b36a8dc95e7e4d028250dffd18e` | not recorded |
| `upstream_fetch.py` | 506 | CLI, spine, module CLI, external, config/contract | keep | 2026-09-30T05:19:04Z | `6311ac155fb57bf962dd156fb9b035daba514cb7` | HAR-110 |
| `verdicts.py` | 527 | CLI, spine, module CLI, external, config/contract | keep | 2026-08-17T22:05:10Z | `40c335ffda95877720e216897798fa4ed925abec` | not recorded |
| `zai_analysis.py` | 953 | tests only | keep: protected | 2026-08-29T23:30:26Z | `25a483dc5b9024caa06cb44875e3f5c61bf5c7d0` | not recorded |
| `zai_campaign.py` | 1375 | tests only | keep: protected | 2026-09-14T23:51:38Z | `d8663d64ef93249078a046367c5d6eef9c0bce5e` | not recorded |
| `zai_report.py` | 337 | tests only | keep: protected | 2026-08-29T23:30:26Z | `25a483dc5b9024caa06cb44875e3f5c61bf5c7d0` | not recorded |
