# ABA per-task LLM audit findings

Rebuild: `uv run evallab failures ingest aba-audits` (optional `--state-root PATH`). Builder: `sources.swe_family.build_aba`.

## Captured sources and custody

Primary public audit root: https://data.autobenchaudit.com/ . Source URLs are retained per finding, under `benchmarks/{tb2,swe_bench_verified,swe_bench_pro}/{static_audits,trajectory_audits}/...json`.

The input is Peter's existing 2026-10-20 normalized label capture, not a freshly invented audit or a bulk import of every source in those files. Initial seed: `~/Developer/research-context/bridge/2026-10-20-open-qa/_lb/{tb2,swev,swepro}-labels.jsonl`. These files mix many auditors; **only `source=aba_static|aba_traj` and `positive=true`** are admitted.

| Capture | Byte count | Raw SHA-256 / revision directory |
|---|---:|---|
| `tb2-labels.jsonl` | 440,545 | `4f96efeb94466ab848ded6d5f48fdc3bbaf0f4f0b6df9b4b376cd2996d3bddf8` |
| `swev-labels.jsonl` | 1,537,345 | `06f115a5588aa38a859fe2e2ee1d28222b448a9e45776c3cc2e85c72bc8ada0b` |
| `swepro-labels.jsonl` | 3,095,186 | `5999bac9cb2306b576ebc2724ca8588fffe2b37b277990d3b6f42538fd386dae` |

Read-only custody: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/aba-audits/<sha256>/<capture-name>`. Default state is `~/.local/state/evallab-failure-library`. Rebuilds work offline from this cache. A fresh installation needs the exact captured seed; mutable live audit URLs are **not** silently substituted if the capture is missing or changed.

Audit-data licence is unstated; `license=null`. Original upstream response hashes/manifests and mapping rationale remain in the existing `_lb/{tb2,swev,swepro}-sources.json` custody notes. Normalized-capture hashes above are not presented as raw-response hashes.

## Canonical task associations, not audited-package equivalence

- TB2: https://github.com/harbor-framework/terminal-bench-2 at `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`.
- Verified: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified at `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`.
- Pro original v1: https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro at `307bd006a0a3807424d3c5525eaf98ca8e63aed3`, **not current default/V2**.

**Every binding is association-only.** ABA's audits do not supply immutable audited task-package revisions. In particular Pro audits describe Harbor-converted material; environment/grader findings do not automatically apply to the native Scale grader. Evidence notes explicitly retain this limit, task digests are null, and current status is unknown. The pinned canonical snapshots are not represented as proof of byte-equivalence.

## Mapping, tiers and splits

Retain the existing source-exported TS crosswalk, one finding per benchmark/mode/task/class. Empty class sets stay `unclassified`; `wrong_fail` maps to `unfair_fail`, `wrong_pass` is unchanged, other directions stay `other`. Negative/minor-only/uncertain rows are not positive findings. Class assignments are best-effort source-subtype mappings, not new independent adjudications.

There are **383 major-positive audit rows**: TB2 21 (5 static, 16 trajectory), Verified 57 (21, 36), Pro 305 (85, 220). Multiple classes yield **441 findings on 297 task IDs**. All are explicitly **UNVERIFIED**, `judge_label`, **V0_unverified**—including trajectory-informed audits that cite runtime observations. None is promoted to executed evidence.

Existing answer-key split aliases are honored with cross-test/heldout/TEST precedence over DEV. New family groups use `terminal-bench/<task_id>`, `swe-bench/<instance_id>` or `swebench-pro/<instance_id>`; `int(sha256(group UTF-8),16) % 5 == 0` selects heldout, otherwise DEV. Measured finding splits: **DEV 327; TEST 17; heldout 97**. Only DEV may feed knowledge; the V0 status must remain visible to consumers.

The builder uses only captured bytes, fixed observation date, stable source-native locators and sorted library output. No benchmark/container/model execution; spend **$0**.
