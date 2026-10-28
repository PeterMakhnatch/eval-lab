# OpenAI 2024 SWE-bench human annotations

Builder: `sources.swe_family.build_openai`; registry collection `openai-swebench-verified-annotations`.

Rebuild: `uv run evallab failures ingest openai-swebench-verified-annotations` (optional `--state-root PATH`).

## Source and custody

- Release: https://openai.com/index/introducing-swe-bench-verified/ . Data: https://cdn.openai.com/introducing-swe-bench-verified/swe-bench-annotation-results.zip .
- Immutable content pin: ZIP SHA-256 `b9297c31f6c2ef712218b5bb2830bd47a7f600dedf6b927191d0170177266ab9`, 2,397,074 bytes; publisher Last-Modified 2024-08-12. The URL itself is mutable; mismatched bytes are refused.
- Read-only raw custody: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/openai-swebench-verified-annotations/2024-08-12/swe-bench-annotation-results.zip` (state defaults to `~/.local/state/evallab-failure-library`). Both ensemble and individual-annotation members are retained, including disagreement; only the ensemble is transformed into findings.
- Task association: `princeton-nlp/SWE-bench@f5351ee8c6663736817027db3ad03fe662cb5bb8`, the historical pre-release original-task snapshot, **not the current 500-task Verified benchmark**.
- Canonical identity check: independently downloaded that revision's 2,294-task `data/test-00000-of-00001.parquet` (12,099,933 bytes, SHA-256 `df1f1b5721a14d2f1b6e4ead68e1bc351b7fdf885065ff0cf56b0994b43c8250`); **all 1,699 annotation IDs are present**. Read-only custody is `raw/swe-bench/f5351ee8c6663736817027db3ad03fe662cb5bb8/data/test-00000-of-00001.parquet`. Membership supports the association, not presentation-byte equivalence.
- **Association-only binding:** the CSV does not pin its audited task-package bytes. A canonical upstream snapshot is recorded, but equivalence to the annotator's exact presentation is unproved. Every evidence note says so; `task_version_digest` is null and `status_now` is `unknown`.
- Annotation licence: not stated in the ZIP. `license=null`; a task dataset's licence is not inherited by the annotation release.

## Mapping and measured counts

Parsed all **1,699** ensemble CSV records with `csv.DictReader` (embedded newlines are not row boundaries). Material severity is 2 or 3, using the source's maximum across three humans. Minor-only ratings and `filter_out` are not separate defects.

- `underspecified >= 2`: **650 TS05** findings, `unfair_fail`.
- `false_negative >= 2`: **1,039 TS07** findings, `unfair_fail`. This is assessed rejection risk, not weak coverage, an executed rejection, or reward-hacking intent; mapping it to TS01 would reverse the meaning.
- `other_major_issues == 1`: **135 unclassified** findings, direction `other`. Free-text rationales are retained without guessing their root cause.
- Total: **1,824 findings on 1,160 tasks**, all `human_label`, **V1_external_label**. Overlapping axes are separate source-native locators, not additional independent tasks. The full individual-rating member stays outside Git; no annotator IDs are exported.

## Split and rebuild rule

The builder first honors `research/answer-keys/splits/assignments.csv`, matching the guard's bare task name (drop benchmark prefix and `@pin`). Existing cross-test/heldout/TEST beats DEV; CheatBench/HackBench key rows cannot become DEV. New groups use `swe-bench/<instance_id>` and `int(sha256(group UTF-8),16) % 5 == 0` for heldout, DEV otherwise. The same group convention is shared with UTBoost, PatchDiff, ABA Verified and ImpossibleBench SWE variants.

Observed split counts: **DEV 1,481; heldout 343**. Only DEV may feed knowledge. Fixed observation date, content pins, source locators and sorted library storage make repeated builds deterministic. No task, grader, container or model was executed; spend **$0**.
