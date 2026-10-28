# UTBoost confirmed weak-task annotations

Builder: `sources.swe_family.build_utboost`; registry collection `utboost-weak-tasks`.

Rebuild: `uv run evallab failures ingest utboost-weak-tasks` (optional `--state-root PATH`).

## Provenance

- Source: https://github.com/CUHK-Shenzhen-SE/UTBoost at commit `4f32782517f23940d3ede8cd1f250667873cc0c6`.
- Artifact: https://github.com/CUHK-Shenzhen-SE/UTBoost/blob/4f32782517f23940d3ede8cd1f250667873cc0c6/assets/useful_scripts/augTest.json . SHA-256 `66cc8846b3e7585853ef57deb39b1698acf4ad17d83c740968e0517fb9e46619`, 83,436 bytes.
- Read-only raw path: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/utboost-weak-tasks/4f32782517f23940d3ede8cd1f250667873cc0c6/augTest.json`; default state `~/.local/state/evallab-failure-library`.
- Task-family association: historical original `princeton-nlp/SWE-bench@f5351ee8c6663736817027db3ad03fe662cb5bb8`. **Association-only**, not a claim that the annotation artifact binds the exact evaluated package. The source JSON lacks candidate patch, task package, reference patch and environment digests; the finding's task digest remains null and current status unknown.
- Canonical identity check: all **36/36** augmentation IDs occur in the independently downloaded 2,294-task historical original parquet, SHA-256 `df1f1b5721a14d2f1b6e4ead68e1bc351b7fdf885065ff0cf56b0994b43c8250`, retained at `raw/swe-bench/f5351ee8c6663736817027db3ad03fe662cb5bb8/data/test-00000-of-00001.parquet`. This is membership evidence, not execution/package equivalence.
- Licence: MIT declared for the source repository; underlying task/test rights still apply. This is not a new blanket licence for inherited task content.

## Evidence and mapping

All **36** source-confirmed augmentation task keys become **TS01 / wrong_pass** findings. Real shape is `{instance_id: {aug_test: string, augFail2Pass: string[]}}`; test bodies stay in immutable raw custody rather than Git. The release lists 43 augmented test entries, including eight empty lists.

All findings are conservatively **human_label / V1_external_label**. The authors report execution and confirmation, but an augmented test patch alone does not reproduce a particular prior wrong pass. Available historical gold/model reports do not establish paired whole-suite success plus candidate-patch identity for all 36 tasks; some whole-task reports are already unresolved. No blanket V3 promotion is made, and generated-test candidates or parser-reannotation rows are not silently added to this collection.

This remains useful historical behavior evidence even if an upstream suite later changed. No `fixed_upstream` claim is inferred merely because an augmented sibling dataset exists.

## Splits and deterministic rebuild

Existing answer-key assignments are checked by the guard's bare task alias, with cross-test/heldout/TEST taking precedence over DEV. New groups are `swe-bench/<instance_id>`; full SHA-256 integer modulo five equals zero for heldout, otherwise DEV. The group is shared across other SWE imports and ImpossibleBench variants.

Measured counts: **36 findings; DEV 25, TEST 4, heldout 7**. Only DEV feeds knowledge. Source-byte pins, fixed observation date and source-native locators rebuild deterministically through the registered builder and sorted `LibraryStore` writer. No benchmark execution or paid calls: **$0**.
