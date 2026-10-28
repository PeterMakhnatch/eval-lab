# PatchDiff published functional wrong passes

Builder: `sources.swe_family.build_patchdiff`; registry collection `patchdiff-wrong-passes`.

Rebuild: `uv run evallab failures ingest patchdiff-wrong-passes` (optional `--state-root PATH`).

## Release and task pins

- Dataset: https://zenodo.org/records/18258368 , DOI `10.5281/zenodo.18258368`, release file `PatchDiff_0115_1.zip` (59,563,518 bytes). Do not substitute older 7z releases.
- Download: https://zenodo.org/api/records/18258368/files/PatchDiff_0115_1.zip/content . Independently downloaded SHA-256 **`32df17a1d86f65c3f7bcebbee8b196467198a4a0ec4b59aa83616426bf2aee1a`**; publisher MD5 also matched `c99c9e842c72ed14b9c206a97e84dff2`.
- Immutable local custody: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/patchdiff-wrong-passes/zenodo-18258368/PatchDiff_0115_1.zip` (default state `~/.local/state/evallab-failure-library`), read-only. Raw candidate patches and tests remain outside Git and are never executed by ingestion.
- **Actual historical task pin:** `princeton-nlp/SWE-bench_Verified@944835eeebcccbe32d1be2f027e696daae60c565`. This exact revision is in the downloaded ZIP's `PatchDiff/data/dataset/swebench_verified/dataset_info.json/download_checksums`, not inferred from a later canonical ID registry. The metadata names `data/test-00000-of-00001.parquet`, 2,091,633 bytes; publisher task parquet SHA-256 is `e43d382299697127527b320062f508aefda4f4909188d94af467dc848079ffb3` (not independently fetched by this import).
- Licence: **CC BY 4.0** for the Zenodo data. No licence is borrowed from the separate GitHub implementation.

## Evidence and counts

The builder verifies the whole archive and these decompressed published RQ1 result members:

| Tool | Member | SHA-256 |
|---|---|---|
| CodeStory | `PatchDiff/results/RQ1_CodeStory_runall.json` | `30b2e153be517c08731a5ba273ebd0df91c509b7fd17d5cce12d5fb5acee033d` |
| LearnByInteract | `PatchDiff/results/RQ1_LearnByInteract_runall.json` | `b6471f1b6a4d66cb3dc08c001d2a908e9a9ca179f273edb4083bfa20cd004cf7` |
| OpenHands | `PatchDiff/results/RQ1_OpenHands_runall.json` | `9e6913c0f169d5d62261ba8266400675cdbda3f531cd6d9cc3a92d5b6465421c` |

Real shape: `{instance_id: {oracle_pass_model_fail: string[], difference: functionality|coding_conventions}}`. Retain only `difference=functionality` with a nonempty test list: **68 task/tool patch findings on 47 distinct tasks**, all **TS01 / wrong_pass**. The tool stays in the locator, so multiple wrong patches on one task are not collapsed. The 36 convention-only RQ1 rows are excluded.

Evidence is **executed / V3_executed**, citing the pinned **published differential execution receipt** in the archive. This is source-reported developer-test execution, **not a fresh Eval Lab replay**, an intent-to-cheat label, or a mere generated test. The published procedure ran oracle and candidate patches and filtered oracle flakiness. Current task status remains unknown.

The alternative `RQ34.csv` contains 77 selected suspicious patch labels: **22 incorrect, four correct, 51 uncertain**, not 77 confirmed wrong passes. It remains in raw custody but is not mislabeled as this collection's wrong-pass denominator. Generated RQ2 differential-test bodies are likewise not automatically evidence of an incorrect solution.

## Split and rebuild

Respect the existing key guard's bare task aliases; cross-test/heldout/TEST beats DEV. New task groups use `swe-bench/<instance_id>`, full SHA-256 integer modulo five equals zero for heldout, otherwise DEV. This group rule is shared with the other SWE collections. Measured split counts: **DEV 49; TEST 2; heldout 17**. Only DEV feeds knowledge.

Pinned bytes, stable tool/task locators, a fixed observation date and sorted library storage make rebuilding deterministic. Ingestion only parsed metadata; no local Docker, models or paid services: **$0**.
