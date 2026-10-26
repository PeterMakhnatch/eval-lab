# LB-2 Goodfire DeepSWE ingestion: reproducible evidence

Authority: `CONTRACT.md` section 4 (Peter Makhnatch, 2026-10-25 afternoon): internal research, agent building, detector benchmarking and trace retrieval on `Goodfire/reward-hack-data`, with the split firewall retained (a task overlapping any test or held-out split stays `held`). `redistribution` stays `not_permitted`; rights are the same owner-authorized internal scope recorded on the earlier Goodfire DEV rows.

## What ran

[`tools/ingest_goodfire_deepswe.py`](tools/ingest_goodfire_deepswe.py) took the three DeepSWE runs (`deepswe-glm-5.2-fp8` 562 rows, `deepswe-kimi-k3` 520, `deepswe-qwen3.8-2.4t-a95b-fp8` 452 = **1,534 rollouts**) from the files LB-1 had already fetched and byte-verified against the pinned Hugging Face revision `bd8fb473568b8ef1d12fc2325a5c8af939c15ad9`. **This item made no network request.** It decoded them with the byte-pinned LB-1 normalizer (`corpus-expansion-lb1-20261025/tools/goodfire_normalize_v3.py`, SHA-256 `79538cd27eed1f924608f4477ea31b04e0135d7501716ccaed64aa6652e67616`, verified before import; no normalizer byte changed). The split was then decided by the production admission gate, the same function retrieval and actor projection use (`admitted_cloud_task_ids` over `CorpusRegistry`), not by a list.

| Result | Value |
|---|---|
| Native rows decoded and written | 1,534 traces, 372,745 steps, 334 task bundles (113 native task names; a bundle is per run) |
| Split by the production gate | **334 DEV tasks / 0 held**; no DeepSWE task overlaps a test or held-out assignment |
| `is_exploit` | 984 of 1,534 DEV traces, set by the existing Goodfire DEV convention: exactly when the publisher's three-vote `verdict_enacted` is `yes`. These are provisional publisher LLM-judge labels (`is_model_exploit_confirmation: false`); nothing was replayed |
| Row counts | `agent_traces` 3,437 to 4,971, `corpus_tasks` 3,373 to 3,707, `trace_steps` 222,510 to 595,255, `task_issues` 252 unchanged |
| Pre-existing rows | Content fingerprint of every other task, trace, step and issue identical before and after |
| Integrity | SQLite `integrity_check` ok, 0 foreign-key violations, FTS integrity-check ok on all four indexes |
| Lossless check | All 372,745 steps were reconstructed from the raw native line in CAS and compared with the stored step; raw-line SHA-256, label-row and passage objects all resolved |
| Backup | Consistent pre-write backup (integrity ok) kept privately; none of it is in this directory |

[`ingestion-receipt.json`](ingestion-receipt.json) has the full counts, per-run decode statistics, source-file SHA-256 values, publisher manifest facts (two of the three runs are marked `_PARTIAL` by the publisher; GLM expected 565 episodes and released 562) and the before/after fingerprints. It contains **no sealed identity**.

## Why the split can be trusted, and its limits

[`verification-outputs.json`](verification-outputs.json) records that the gate, applied to the existing 809 Goodfire task rows as if all were DEV, reproduces the existing partition exactly (714 DEV / 95 held), and still does after this ingestion (1,048 / 95). A sensitivity probe wrapped each of 1,337 sealed text identities in this run's id shape and confirmed the gate admits none of them, so DeepSWE-shaped ids are matched by bare name. That is evidence about name and ledger matching only. DeepSWE names live in a kebab-case namespace shared with Terminal-Bench-style names; LB-1 held them for that reason, and the gate is the first check that compares them with the ledgers. A task whose identity appears in no ledger would still be admitted; the firewall is only as complete as the split ledger (`sha256:542f28de8fc6c88a8a4667cfe6a57f07788f06757af5be29706db835d7eb4a86`).

**Retrieval caveat.** All 334 tasks are admitted by the retriever (3,418 admitted tasks in total), but `retrieve_similar_examples` returned no example for ten queries, including one that names a pre-existing Goodfire task. 751 of the 1,048 Goodfire DEV task ids contain a bare numeric seal (108 numeric seals exist) as a substring, which is the known `sealed_identifiers()` false-seal defect tracked as Priority #5. That diagnosis is an inference; I did not remove the seals to test it. This item changed no retrieval code.

## Source-I/O limitation (unchanged from LB-1)

LB-1's child fetched all nine Goodfire streams before its alias gate, so zero-heldout-read compliance for those downloads cannot be certified. This item reused those verified bytes, read the split ledger through the production gate (identities only; no ledger or sealed identifier is printed or stored here), and did not open task or answer bodies of any sealed split.

## Script revisions, locks, and rollback

The decode phase ran under an earlier revision of the script (`ingest_script_sha256` `04ff6f15b4bec6adc60823863750e05cb0ae1139fa58db01866ce2ff72724ecc` in the receipt's `decode` block; those bytes were not kept). Later revisions, to the committed `adc05342ee11d4c2f5b9326d0da1fa66c491e385edb4da0b720d0740a852f89c`, added the sealed-name sensitivity probe, a retry proxy for SQLite lock contention (other agents read this rollback-journal database for long stretches; the first apply attempt failed with `database is locked` before writing anything), and reuse of the pre-write backup. The decode function itself was not edited.

To undo: the private backup `cloud-trace-db.pre-lb2.sqlite` restores the 3,437 / 3,373 / 222,510 state. Restoring it discards any rows other writers add afterwards, so prefer deleting only the 1,534 traces and 334 tasks (the traces carry run ids `goodfire-reward-hack-data@<revision>/deepswe-*`, and the 334 tasks are the tasks those traces reference).

## Not claimed

Not a clearance for redistribution, training admission, or actor exposure; the actor path is `evallab prod project-actor`. Not a refresh of the identity-only held inventory (`corpus-expansion-lb1-20261025/identity-manifest-receipt.json`), which still lists the 113 DeepSWE aliases as held; those aliases are now DEV here by owner authorization and the gate. Not a replay or confirmation of any source label. The files under `tools/` other than the ingest script are the throwaway verification scripts that produced `verification-outputs.json`; they hard-code this machine's database path and are evidence, not supported tooling. This directory is under the existing immutable run-evidence lint exclusion.
