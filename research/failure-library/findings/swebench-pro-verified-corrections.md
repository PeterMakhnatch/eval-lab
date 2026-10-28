# SWE-bench Pro expert-approved corrections

Builder: `sources.swe_family.build_pro_corrections`; registry collection `swebench-pro-verified-corrections`.

Rebuild: `uv run evallab failures ingest swebench-pro-verified-corrections` (optional `--state-root PATH`).

## Source and custody

- Source: https://huggingface.co/datasets/opencompass/SWEBench-Pro-Verified . Corrected dataset revision: `ae466f824cdee69d2c010b6c954b7418c411b6e4`.
- Source-native assignment lists: https://huggingface.co/datasets/opencompass/SWEBench-Pro-Verified/blob/ae466f824cdee69d2c010b6c954b7418c411b6e4/README.md . Downloaded README SHA-256 `ac6a2ef14e99d1b2ef33c17a8fe3448c05e70bb66c4f742448e48c50f7b8a53a`, 14,636 bytes.
- Raw custody: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/swebench-pro-verified-corrections/ae466f824cdee69d2c010b6c954b7418c411b6e4/README.md`, read-only after download. Default state is `~/.local/state/evallab-failure-library`.
- Broken task version: https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro at original v1 tag commit `307bd006a0a3807424d3c5525eaf98ca8e63aed3`, 731-task cohort. **Not the newer 642-task default/V2 benchmark.** Original parquet content SHA-256, retained prior intake: `c8cd7115496ad4e9a8b21d088cef576a65bf821bb542b24336f13f714cef13f8`.
- Licence: source dataset declares Apache-2.0. Source labels describe LLM-assisted proposals approved by human experts, not independent Eval Lab certification.

## Mapping and counts

The four README code blocks contain **102 distinct task-ID assignments**; declared block counts and duplicate identities are checked. Mapping follows the README's definitions, not the intuitive English meaning of “narrow.”

| Source category | Count | Class | Direction |
|---|---:|---|---|
| Misleading prompts | 22 | TS06 | unfair_fail |
| Overly narrow tests: requirements neither stated nor inferable | 75 | TS05 | unfair_fail |
| Overly borad tests (upstream spelling): requirements not covered | 3 | TS01 | wrong_pass |
| Other issues | 2 | unclassified | other |

Each record keeps `human_label` plus `upstream_fix` evidence and **V2_upstream_fixed**. `fixed_in_revision=ae466f824cdee69d2c010b6c954b7418c411b6e4` identifies the **opencompass corrected dataset**, as the evidence URL states; it is not represented as a Scale V2 commit. Status is `fixed_upstream` for this published correction, without claiming the repaired task was executed or is fully defect-free.

Prior source intake compared the 102 labelled tasks against original Scale v1 and observed changed inspected fields on all 102 (83 instruction-only, 14 instruction+grader, five grader-only). This builder reuses the authoritative label lists rather than treating every changed or unlabelled task as a defect.

## Splits and rebuilding

Honor existing key assignments by bare task name, with non-DEV precedence. New groups are `swebench-pro/<instance_id>`; `int(sha256(group UTF-8),16) % 5 == 0` is heldout, otherwise DEV. This grouping matches ABA Pro. Measured splits: **DEV 83; heldout 19**. Only DEV feeds knowledge.

The registered builder validates pinned raw bytes and emits stable source-native locators; `LibraryStore.write_findings` sorts deterministic output. No repaired grader was run; spend **$0**.
