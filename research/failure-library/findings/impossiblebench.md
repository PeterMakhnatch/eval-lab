# ImpossibleBench constructed variants and matched original controls

Rebuild: `uv run evallab failures ingest impossiblebench` (optional `--state-root PATH`). Builder: `sources.constructed.build_impossiblebench`.

## Sources and immutable bytes

| HF repository | Full revision | Raw variants | Base tasks |
|---|---|---:|---:|
| https://huggingface.co/datasets/fjzzq2002/impossible_swebench | `9c2d34f364b7229e8c0ff807c646100bdc18bbb5` | 1,047 | 349 |
| https://huggingface.co/datasets/fjzzq2002/impossible_livecodebench | `98650ffc3f28a01b261669b6d19fcd7773823710` | 309 | 103 |

All **1,356 rows** were freely downloaded at these revisions, including every original control. Download form: `https://huggingface.co/datasets/<repo>/resolve/<revision>/data/<variant>-00000-of-00001.parquet`. Independently hashed raw bytes are retained read-only under `$EVALLAB_FAILURE_LIBRARY_STATE/raw/impossiblebench/<revision>/data/` (default state `~/.local/state/evallab-failure-library`).

| Source | Variant | Rows | SHA-256 |
|---|---|---:|---|
| SWE | original | 349 | `4ad148f9812e9a347ec529690bff8fed49b8ec7d0256e3b6768d375fed499f3b` |
| SWE | oneoff | 349 | `6308b6fa360213dc40a5bb5e39217efcc440f48c76b8caaf00bbf3497cb67462` |
| SWE | conflicting | 349 | `25d96f864431f9279375c94df05ffdaeeafbcacde0be8b2b0a0333daefa4186d` |
| LiveCodeBench | original | 103 | `3f128b7a8419fd4f2d1c11f3eca7cd5bceac0e2385810950e926276d07a784c5` |
| LiveCodeBench | oneoff | 103 | `31a89b06195a223268c4e66ea9ae87c5ed5b744bb7d230ed950d2e012c2c6c56` |
| LiveCodeBench | conflicting | 103 | `9a13116c6cf48e8c2dc64ca2facdac4122aad30fe1aa52ab552a3b35638c22c8` |

Both HF datasets state **no licence**; `license=null`. The harness's MIT licence is not transferred to these datasets or their source issues/problems.

## Finding semantics

Only the **904 modified variants** become findings: 698 SWE and 206 LiveCodeBench. `oneoff` maps to **TS06** (deliberately changed expectation versus intended specification), `conflicting` to **TS10** (mutually incompatible grading conditions under the intended deterministic-function contract). All are `construction`, **V1_external_label**, direction `unfair_fail`, status unknown. Construction is not proof of a rewarded hack, natural task-defect prevalence, or an observed run outcome. No harness permissions or TS04 claim are inferred from membership.

TaskRefs pin the constructed HF dataset itself, not an assumed original SWE/LiveCodeBench revision. `source_id` is `impossible-swebench` or `impossible-livecodebench`; `task_id=<base-id>/<variant>`. Task paths point into the exact parquet and identity column. This keeps variants independently linkable while retaining their common family.

## Counted, linkable matched-negative set

The **452 `original` rows are controls, not findings**. “Control” means no construction defect was injected; it is not independent certification that the underlying task is flawless. They are available to the loop as matched original negatives:

| source_id | Revision | Original-control member | Identity column | Count | DEV / TEST / heldout |
|---|---|---|---|---:|---|
| `impossible-swebench` | `9c2d34f364b7229e8c0ff807c646100bdc18bbb5` | `data/original-00000-of-00001.parquet` | `instance_id` | 349 | 223 / 67 / 59 |
| `impossible-livecodebench` | `98650ffc3f28a01b261669b6d19fcd7773823710` | `data/original-00000-of-00001.parquet` | `task_id` | 103 | 87 / 0 / 16 |

To join a finding to its original: read its pinned dataset's original-control member, match the base ID before the final `/oneoff` or `/conflicting`, and construct control `task_id=<base-id>/original`, `task_path=data/original-00000-of-00001.parquet#<identity-column>=<base-id>` using the **same source_id, upstream_repo and revision**. The control parquet digests above bind the full counted set; no unpinned `latest` lookup is needed. Total controls by split: **DEV 310; TEST 67; heldout 75**.

Programmatic read-only projection: `sources.constructed.impossible_controls(root, state_root=None)` returns the 452 controls with `task: TaskRef`, split, canonical group, source locator, pinned `label_source`, `artifact_sha256`, and `raw_row_digest=sha256(canonical full source-row JSON)`. `label_kind=construction` and `label=matched_original_control` explicitly distinguish these from executed-clean certification. Test/code bodies are not returned. The row digest is separate from `TaskRef.task_version_digest`, which remains null; it must not be mistaken for an Eval Lab task-directory digest.

## Leakage and runtime limits

Apply existing answer-key bare-task assignments before hashing; reported/cross-test assignments beat DEV. All variants, including controls, use the same base task group: `swe-bench/<instance_id>` or `livecodebench/<task_id>`. New groups use full SHA-256 integer modulo five: zero is heldout, otherwise DEV. Measured finding splits: **DEV 620; TEST 134; heldout 150**. Only DEV feeds knowledge. The three variants are not independent discoveries.

The LiveCodeBench harness had a public port bug in test-modification forwarding, fixed at `safety-research/impossiblebench@74ae2e852c27a982e2a2823acdd4e547219d6323`. Dataset pins alone do not freeze mutable images or runtime permissions. No task or corpus program was executed by ingestion; spend **$0**.
