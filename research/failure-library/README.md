# Failure Library

Source-backed task-defect findings and failed or reward-hacked run observations, preserved for defensive Task QA and Trace QA. Historical failures remain useful after a task is fixed; source claims are not automatically independent verification.

- **Contract:** `failure-library/v1` in `src/evallab/failure_library/records.py`.
- **CLI:** `uv run evallab failures --help`.
- **Coordination:** `~/Developer/research-context/bridge/2026-10-27-failure-library/CONTRACT.md`.

## What a record says

- **Finding:** one source claim or constructed defect about a pinned task version.
  - It names the upstream repository and revision as observed. Some sources bind task IDs to a reference snapshot by association only; their collection notes disclose this instead of claiming byte-identical audited tasks.
  - It records the defect class (TS01–TS11), evidence, finder, source, and current status. `unknown` is not “still broken.”
- **RunRecord:** one source-export observation of an agent attempt.
  - Holds source-native labels and their producer, plus a transcript locator. Different source exports can overlap; row counts are not independent replications.
  - A task that was fixed later does not make its runs useless: they still show model behaviour.

## Verification tiers

The tier describes the strongest recorded **support**, derived from evidence kinds. It does not certify a fresh Eval Lab replay, an exact historical task binding, or present-day breakage. A reproducer path alone is not execution proof; collection notes distinguish published receipts and historical local receipts.

| Evidence kind | Tier | Example |
|---|---|---|
| `executed` | V3_executed | historical receipt or publisher-reported executed reproducer, explicitly attributed |
| `upstream_fix` | V2_upstream_fixed | the maintainers' fix commit, pinned |
| `human_label`, `construction` | V1_external_label | a human audit; ImpossibleBench's impossible-by-construction variants |
| `judge_label`, `static_finding` | V0_unverified | an LLM judge vote; a static scan |

`stats` separates findings, source-scoped task identities, task versions, construction,
evidence kinds and receipt/reproducer references. These are different denominators.
Label-occurrence counts are separate from distinct runs carrying each label kind.
Model agreement, regex signals and mined fix candidates never become V3/V2 proof.

## Where things live

| What | Where |
|---|---|
| Findings (small, reviewed) | `findings/<collection>.jsonl`, sorted by `finding_id` |
| Run manifests (count, sha256, source revision) | `runs/<collection>.manifest.json` |
| Run records (large) | `$EVALLAB_FAILURE_LIBRARY_STATE/runs/<collection>.jsonl` (default `~/.local/state/evallab-failure-library`) |
| Raw source bytes | `<state>/raw/<source>/<revision>/`, read-only after download |
| Normalized history (old and new bytes) | `<state>/history/`, using the existing evidence CAS |

Every collection/manifest write archives both prior and replacement bytes before
advancing the latest pointer. Given a JSONL SHA-256 from a retained manifest,
`evallab.evidence_store.load_blob(state / "history", "cas://sha256/" + digest)`
recovers those exact normalized bytes. Findings retain their upstream revision;
Git retains reviewed manifests and collection-note history. Immutable raw source
custody remains the authority, not a mutable latest view.

## Leakage rules

- Only `split = DEV` items may feed knowledge packs or agent retrieval.
- Cross-test sets (CheatBench, HackBench) and any key's TEST or heldout items are recorded as `cross_test`, `TEST` or `heldout`. They are never compiled into knowledge.

## Run it

```sh
uv run evallab failures --help
uv run evallab failures ingest pm-tb4-findings
uv run evallab failures runs verify
uv run evallab failures stats --json
```

Each collection's adjacent Markdown note gives its exact pinned source, licensing,
evidence limits and deterministic rebuild command. Large normalized runs and raw
transcripts stay outside Git. Ingestion parses data; it does not execute imported
patches or test bodies.

## Defensive scope

Knowledge exports support read-only Task QA and Trace QA. They must distinguish
retrieved DEV examples from generic defensive taxonomy guidance, withhold unknown
cross-publisher task-family matches, and preserve citations and exclusion manifests.
They do not export attacker recipes or launch autonomous reward-seeking/re-break
campaigns. Monitoring-awareness mentions are observational, not evidence that
monitoring caused a model to avoid reward hacking.
