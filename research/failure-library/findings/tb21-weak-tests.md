# tb21-weak-tests

Rebuild: `uv run evallab failures ingest tb21-weak-tests`.

- Source: `research/answer-keys/tb21-weak-tests-v1`, the existing instruction-adjudicated native changed-line sweep. Task repository/revision: `harbor-framework/terminal-bench-2-1@7131e4375048a0e408a8fb404b5f499d726b695b`. Exact task-package digests are copied from key items.
- Input SHA-256: `items.jsonl` = `102e0688ed9016034bae6a330a7a8d9f07c0f988466462fd36748cfadee0f18e`; `atlas.jsonl` = `3fb61420b7e28a7b57465aabda507ee21d00635f0fcca9f16240dcdc16a5082a`; `findings.jsonl` = `ec82a952607aef1d3134e6b500ddf5da01a357d2cd204c5c4501dc2ad2ba0271`.
- Count: **14 findings / graded witnesses on 5 confirmed weak tasks**: cobol-modernization, make-mips-interpreter, multi-source-data-merger, sam-cell-seg, sqlite-db-truncate. The remaining 84 tasks are not converted into either broken-task findings or clean negatives.
- All **14 are V3_executed** in this publication. Intake checks the four native archived control receipts: original-reference 1, no-op 0, from-base full-targets 1, witness 1; each control's package binding, isolation, timeout-preservation flag, receipt SHA-256 and actual stored trial reward must agree. It does not run Docker or replay a grade.
- If the native receipts are missing, the builder emits only `static_finding` / V0. Changed receipt bytes or inconsistent stored rewards are a provenance error, not a reason to silently retain V3.
- Mapping: confirmed required behavior escaping the verifier → **TS01**, `wrong_pass`. Locator is the original `qa.finding/v1` finding ID; receipt and witness references remain repository-relative.
- Splits are copied per task from the key: **5 DEV / 9 TEST findings**. The source-visible search is not an independent detector evaluation. No new split rule or upstream-fix claim is introduced.
- License: task dataset **Apache-2.0**; retained evidence stays under its existing repository/source licensing.
