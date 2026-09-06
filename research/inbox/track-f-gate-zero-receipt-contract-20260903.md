---
source_type: internal
---

# Track F — Gate Zero source-receipt and atomic-publication contract (2026-09-03)

Scope: PR #359 repair (`1011f586` branch) and every later consumer of real evidence (Tracks A–D). Answers wK:p7's two architecture questions and defines the minimal receipt/parity contract. Binding alongside `track-f-architecture-review-20260903.md` §3 (Gate Zero: UNSATISFIED / BLOCKING).

## 1. Receipt type — REUSE the G7 authority family; mint no second authority

The source receipt is a thin, frozen, additive wrapper around the existing trial-admissibility authority, not a new authority:

- Required core: a `TrialAdmissibilityV1`-verified record or `finalize_trial_admissibility` output (`trial_admissibility.py:551-576, 606-659`), referenced by its own digest, plus the bound `TrialSourceDigests`-family source identities from the G7 source resolver (`trial_admissibility.py:146-193`).
- Receipt fields: `admissibility_record_digest` (sha256 of the verified authority record), `source_kind`→`sha256(actual_bytes)` map for every bound artifact the consumer reads, `cas_record_kind="source-receipt"` (new additive CAS record kind per Track F §1.3), `consumer` (module+version digest), `created_at`.
- Validation the exporter performs before any manifest row is minted:
  1. the referenced admissibility record EXISTS, parses as `TrialAdmissibilityV1`, and its verify status is affirmative (positive evidence; absence/unknown refuses — Track F §2c);
  2. for each bound artifact: `sha256(source_bytes) == declared digest` — a forged-but-self-consistent CAS/lineage string with wrong bytes is a typed refusal (`receipt_digest_mismatch`). Strings alone never authorize (wK:p7 probe finding, confirmed);
  3. CAS identity is derived from bytes (content address), never accepted from caller strings;
  4. contradiction refusal: a receipt whose lineage record contradicts another receipt or the bound digests refuses (`receipt_contradiction`).
- This is exactly the Track F §3 replacement sidecar: additive record kind, content-bound, future-emitter compatible (a receipt may exist before the 128 backfills resolve; it simply cannot authorize those trials), and downstream consumers read `TrialAdmissibilityV1`-verified state only.

## 2. Publish boundary — keep stage/fsync/rename INSIDE the exporter

Do not extract a shared atomic-write utility:

- The atomicity unit is the whole-dataset directory (manifest + JSONL + exclusions), which is export-specific; a generic "atomic write file" utility would invite reuse for mutable artifacts and erode immutability-by-construction. Repo precedent keeps durability primitives local to their store (`evidence_store.py` staging/inventory, `campaigns.py` `CampaignStore` tmp+rename snapshot). Extract a shared helper only when a real second consumer appears (operating model: no seam without a second production implementation).
- Required semantics (fail-closed):
  1. stage in a NEW sibling directory on the same filesystem (same-volume rename only);
  2. fsync every staged file, then fsync the staging directory (dirent durability);
  3. refuse if the destination exists (regular, symlink, or directory — any kind);
  4. exactly one `os.rename(staging, destination)`; no merge-into-empty-dir semantics: the destination must not exist beforehand, and a post-rename relist must confirm the published set matches the staged inventory;
  5. any failure before rename leaves only the staging directory (cleanable, never authoritative); failure at rename raises and publishes nothing partial.
- Immutability: published destination is never overwritten, rewritten, or appended; a re-export with different content publishes a new content-derived destination or fails; same-content re-export is byte-identical (Track F §7.6).

## 3. Parity and canonical-set requirements (binding on #359 repair)

- Logical sets (suite membership, task set, backends, exclusion reasons, tool IDs) serialized sorted+unique; reorder or alias with a recomputed digest refuses (`canonical_set_mismatch`).
- Manifest split/exclusion paths use exact path constants; no alias spellings.
- Message sequence `== range(n)` contiguous; tool_call IDs unique; every tool response links to its immediately declared preceding call; dangling calls refuse.
- `invalid_source_digest` stays a typed exclusion for None/malformed digests — it excludes the record; it never downgrades to a warning path that still mints rows.
- Rehydration: a re-loaded manifest re-verifies receipt digests + canonical sets; self-digest alone insufficient (same rule as PR #355 R1).

## 4. Review requirement added for #359 repaired-head pass

The repaired head MUST include negative controls: (a) forged CAS string + self-consistent lineage where bytes digest ≠ declared digest → `receipt_digest_mismatch`; (b) missing/unknown admissibility record → typed refusal; (c) existing destination (file, symlink, dir) → refusal, zero writes to it; (d) reordered set with recomputed digest → `canonical_set_mismatch`; (e) non-contiguous sequence / dangling tool call → typed refusal; (f) partial-publish simulation (staging left behind) → destination absent.
