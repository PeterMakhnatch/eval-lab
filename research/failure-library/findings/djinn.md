# Djinn constructed exploitable insecure verifiers

Rebuild: `uv run evallab failures ingest djinn` (optional `--state-root PATH`). Builder: `sources.constructed.build_djinn`.

## Source and custody

Source: https://huggingface.co/datasets/EleutherAI/djinn-problems-v1.0 at full revision **`8c752336b9dda0339bbb652825691689a496d387`**. All three pinned parquet files were downloaded freely and independently SHA-256 hashed. Download form: `https://huggingface.co/datasets/EleutherAI/djinn-problems-v1.0/resolve/<revision>/data/<split>-00000-of-00001.parquet`.

| Source split | Rows | Bytes | SHA-256 |
|---|---:|---:|---|
| train | 1,032 | 3,007,056 | `efa550d73c930a0b86399fb23822315e758309b8490f49697003a18696ba1d66` |
| coverage | 110 | 349,434 | `a2752bf25bc841944ec735ba40f71f3934f9abe3f1cc5d6f9f1be139610a2968` |
| gradient | 82 | 327,991 | `a0d9533c6953eb27856a79fae7f26fa051fcc05abf6a96bbc9173097d5a47afb` |

Read-only raw path: `$EVALLAB_FAILURE_LIBRARY_STATE/raw/djinn/8c752336b9dda0339bbb652825691689a496d387/data/` (default state `~/.local/state/evallab-failure-library`). TaskRefs use the source HF repository, full revision and exact split/id locator. Code, test cases, candidate exploits, secure reference solutions and provenance JSON stay in raw custody, not Git; the builder reads only metadata/grouping columns.

Dataset card licence: **Apache-2.0**, reflected in records. This declaration does not prove the complete inherited TACO/source-problem rights chain is cleared; upstream per-problem attribution is incomplete. No separate code-repository licence is borrowed.

## Mapping and evidence

All **1,224 source rows** become `construction` findings, **V1_external_label**, direction `wrong_pass`, current status unknown. The intentionally insecure verifier remains the subject even when the row has a hardened secure companion; a companion is not represented as an in-place upstream repair.

- Explicit tamper mechanisms `test_skipping`, `verifier_logic_override`, `result_manipulation`, `environment_variable_manipulation` map to **TS04**.
- Other source exploit types map conservatively to **TS01**, with the original `exploit_type` retained in the summary. This is a coarse weak-verifier crosswalk, not proof every source mechanism is grader tampering. Coverage's 110 weak-suite problems remain distinct from train's mechanism-bypass cohort.
- Gradient's 82 `resource_exhaustion` problems are a fail-open continuum; no intentional-hacking run label is created.
- The publisher's hardening/screening/probe-vector claims do **not** become fresh executed evidence. Dataset programs were neither imported nor executed.

## Template grouping and deterministic splits

Raw IDs and source train/coverage/gradient buckets are **not** independent templates. Confirmed aliases include train/coverage versions with unrelated suffixes and coverage duplicates. The builder groups by:

`sha256(normalize_whitespace(description) + "\x1f" + function_name)`.

Mechanism, insecure test cases, source split, `fix_json.group` and ID suffixes are deliberately excluded. On the pinned bytes, **1,224 rows form 1,174 such content groups**. Every alias in a group gets the same new-source split, including across source splits. This exact-content grouping is conservative; it does not claim semantic near-duplicate detection.

Honor existing answer-key bare-ID assignments, with non-DEV precedence. New group key is `djinn/<content-fingerprint>`; `int(sha256(group UTF-8),16) % 5 == 0` is heldout, otherwise DEV. Measured counts: **DEV 981; heldout 243**. Source `train` does not automatically mean Eval Lab DEV. Only DEV feeds knowledge.

Pinned bytes, a fixed observation date, source-native split/id locators and sorted storage make rebuilding deterministic. Spend **$0**; no local Docker or LLM inference.
