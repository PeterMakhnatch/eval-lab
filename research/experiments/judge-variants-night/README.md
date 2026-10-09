# MiMo general/webdev judge variants — $0 overnight receipt

## Outcome and qualification boundary

**MEASURED:** 3,018 final content-addressed candidate packages: **925 general + 2,093 webdev**, selected in [`packages.jsonl`](packages.jsonl), with task-level decisions in [`ledger.csv`](ledger.csv). The immutable HF snapshots were not edited. The six transforms are registered in `evallab.hardening`.

These are repaired **candidates**, not a claim of 3,018 good RL tasks. **921 general tasks still need judge/oracle validation; four are quarantined for seven rule runtime errors. All 2,093 webdev tasks are staged pending a version-aware judge deployment and oracle false-rejection/reliability tests.** No model/judge calls were made; measured spend is $0. No images were pulled. Controls used one owned, `--rm`, `--network none` container at a time.

Pinned inputs: `FineEnvs/MiMo-V2.6-RL-harbor-general@10b732c5079c47244a77402f5759d62763800f20`; webdev is the `e1a6293376e8` snapshot with its full revision retained in lineage. Workplace bytes are fetched from `XiaomiMiMo/MiMo-V2.6-RL-oss@639865fd3374018d6cb29b9fb82dd531406fcf5f`, checked against manifest SHA-256 or Git blob SHA-1, including cached files.

## Applied repairs

| Transform | Final fleet applications | Behavior and limit |
|---|---:|---|
| `general-pinned-backup@1` (G2) | 50 | Consistent SQLite backup after MCP startup and before agent readiness; includes committed WAL state and fails setup closed if no/read-failing DBs. Fifty tasks contain references; this is not a claim that all fifty runtime paths are broken. |
| `general-strict-answer@1` (G4) | 925 | Missing `answer.md` stays missing; other renamed deliverables retain the existing fallback. Final chat materialization is unchanged. |
| `general-nop-zero-weight@1` (G3) | 197 | Zero weights for **269 rule atoms measured awarding pristine-state credit**. Checks still execute and report failures. Their preservation behavior is audit, not reward. |
| `webdev-temp0-pin@1` (W2) | 2,093 | Temperature zero, 1,024 output-token cap, fixed Maverick-FP8/Novita request identity and rubric record; absent/mismatched deployment or response artifact revision masks instead of scoring. Temperature zero does not guarantee repeat agreement. |
| `webdev-structural-gate@1` (W3) | 2,093 | Rendered DOM must be nonblank, contain at least one selected brief keyword, and avoid horizontal overflow. Hidden source keywords do not count. Missing browser measurements mask; they do not pass. Necessary gates are **not** semantic completeness or an aesthetic oracle. |
| `webdev-brief-explicit@1` (W1) | 632 | The **entire** long brief reaches the judge, with full SHA-256/length provenance. No silent 1,500-character tail drop, and no claim that keywords enforce omitted requirements. Maximum measured brief length: 12,173 characters. |

**SOURCE-QUOTED:** G1's MCP compatibility fix is already present in every inspected pinned general setup: `environment/setup/setup.sh:15–18` prepares `mcp==1.26.0` in `/opt/openai-agents-venv`. The cached image itself has `mcp==2.2.0`; replacing the image alone is not the setup fix. No redundant G1 transform was added.

Pilot lineage records from earlier unpublished candidate shapes remain immutable, but are **not activation selectors**. Use only the final digest/chain in `packages.jsonl`, not the newest filename or a transform name alone.

## Full deterministic rule-only census

**MEASURED command:**

```sh
uv run --with openpyxl --with python-docx --with python-pptx python research/experiments/judge-variants-night/build.py --census --derive
uv run python research/experiments/judge-variants-night/summarize.py
```

Retained item results: [`rule-census.jsonl`](rule-census.jsonl); aggregate: [`summary.json`](summary.json). The corpus has **688 rule atoms in 230 tasks**, and **695 tasks with no rules**. Every rule atom ran in its own subprocess against a hash-verified pristine replica. The 695 no-rule floors are statically zero by the unchanged reward formula; their workplace bytes were not downloaded just to prove zero rules. Filesystem and module-import/runtime failures are recorded separately from negative rule results.

| Rule-only floor, text-judge scores fixed to zero | Tasks with positive floor | Mean over all 925 | Maximum |
|---|---:|---:|---:|
| Before G2/G3 | 176 | 0.0481636 | 0.6 |
| After G2, before G3 | 197 | 0.0540139 | 0.6 |
| After G2 + G3 | 0 | 0 | 0 |

G2 changes the measured floor of 27 tasks. Repairing missing baseline comparisons can **increase** do-nothing credit; this is why G3 is measured *after* G2, rather than declaring backup creation alone a quality fix.

**Limit:** this is the deterministic rule contribution/lower bound under zero text-judge scores, **not a full observed nop reward**. The full nop can still earn text-judge credit; detecting that is in the staged campaign. Seven remaining rule errors on four tasks (missing tables/column) are preserved and quarantined in `ledger.csv`; no catch-and-pass suppression was introduced. An initial host dependency mismatch was corrected with the same document-reader packages needed by the checks, then only dependency-failing tasks were recomputed. The final census has no download or missing-module errors.

## Real offline environment controls

**MEASURED:** `uv run python research/experiments/judge-variants-night/general_controls.py`, [`general-control.json`](general-control.json), **n=1 general task**, `s3k_0067_accounting_audit_tax_en_t3_rl_007` on the pinned cached general image. Setup completed; MCP ports 39101–39104 each returned **seven tools**; **seven live system DB backups** passed SQLite integrity checks. Host-verified downloads and a retained wheel-hash manifest replaced only network download/install transport in the copied setup; MCP startup, isolation, G2 snapshot and ready sentinel ran inside Docker with network disabled. This is not a 925-environment startup sweep.

**MEASURED:** `uv run python research/experiments/judge-variants-night/controls.py`, [`browser-controls.json`](browser-controls.json), **n=1 webdev brief, four HTML controls**, real changed renderer/grader on the pinned cached webdev image:

- Blank rendered DOM: reward **0**, before inference.
- Unrelated page with the right name only in `display:none`: reward **0**, before inference.
- Visible correct keyword with 3,008px scroll width against a 1,440px viewport: reward **0**, overflow gate.
- Necessary-gates-passing page: reward **null**, **not scored**, because the deployment revision is unbound. No fake judge score and no paid POST.

A keyword dump may pass the necessary gates. Honest oracle pages at mobile/tablet/desktop sizes and semantic negative controls are still required before these gates are accepted for RL reward.

## Staged judge reliability and verified price

[`reliability-spec.json`](reliability-spec.json) is disabled and approval-required; [`reliability-cohort.json`](reliability-cohort.json) names 50 general and 50 webdev candidate digests. It specifies reviewed oracles, six artifact/control conditions, five repeats, repeat-agreement/mutation-gap/mask thresholds, and a smaller three-repeat pilot before expansion. Neither domain ships an oracle, so none was fabricated.

**MEASURED:** the free public [HF router model catalogue](https://router.huggingface.co/v1/models), fetched 2026-10-09, reports the following exact upstream model/provider rates in **USD per million input/output tokens**, retained in [`pricing.json`](pricing.json):

| Model/provider | Input | Output |
|---|---:|---:|
| `thinkingmachines/Inkling:deepinfra` | $0.95 | $4.05 |
| `thinkingmachines/Inkling:together` | $1.00 | $4.05 |
| `meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8:novita` | $0.27 | $0.85 |

**SOURCE-QUOTED:** [HF's pricing policy](https://huggingface.co/docs/inference-providers/pricing) passes through provider prices without markup. The catalogue reports no price for Inkling/Fireworks; no rate was invented. It exposes model/provider IDs, **not a deployed artifact revision pin**. A Hub commit identifies model bytes, not what a floating router serves. W2 therefore refuses unversioned deployments/replies. A real version-aware endpoint remains a prerequisite; adding a fake `model_revision` field to a forwarding shim would not satisfy it.

**[INFERENCE]:** at 6,000 general calls with 6,000 input/200 output tokens each plus 1,500 webdev calls with 12,000 input tokens (including image) and 512 output tokens each, the catalogue-rate projection is **$44.57**. Four attempts at those same token sizes project **$178.29**, not a hard upper bound. If every output hits its configured cap, successful-call projections become **$131.40 general + $6.17 webdev**. Actual image accounting, retry billing and a newly version-pinned endpoint's quote are unmeasured. Refresh prices and obtain approval for the real bounded endpoint/spec before any paid call. No credit balance was treated as permission or free money.

## Focused verification contract

Behavior tests cover SQLite committed-WAL snapshotting/refusal, strict missing-answer evidence, positive-rule-only credit removal/refusals, rendered-DOM gates/masks, complete long-brief transport, and deployment/response revision binding. They require no Docker, network or clock. Repository checkpoint:

```sh
make prepush TESTS='tests/test_judge_variants_general.py tests/test_judge_variants_rule_floor.py tests/test_judge_variants_webdev.py'
```

**MEASURED:** the checkpoint passed ruff and docindex, then stopped at the expected stale `docs/repo-map.md` freshness check. The integration owner explicitly required leaving generated repo-map/INDEX untouched for one morning regeneration; this is an expected red gate, **not CI green**.

The remaining commands were run individually: `uv run --no-sync python -m evallab.governance check`, `uv run --no-sync evallab registry audit --json`, `uv run --no-sync python -m evallab.lessons`, and `uvx ty@0.0.71 check src/ --output-format concise` passed. Registry audit retains existing legacy-certification warnings. `uv run --no-sync ruff check .` and the focused pytest command above (substituting `uv run --no-sync pytest` for `make prepush TESTS=`) passed: **33 tests**. No full suite claim.

`build.py --derive` compiled every final package's Python test entrypoint: **3,018 files, three distinct source bodies, zero syntax errors**, recorded in [`package-validation.json`](package-validation.json). The complete-brief consumer test caught an invalid inline comment before delivery; it was removed, all 632 selected long-brief packages were regenerated, and final cohort digests follow the corrected packages.

Generated catalog/INDEX integration belongs to the parent; this slice does not edit `docs/mimo-task-catalog.md` or INDEX files and does not merge its PR.
