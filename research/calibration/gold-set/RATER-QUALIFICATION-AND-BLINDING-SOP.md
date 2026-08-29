---
type: sop
topic: rater-qualification-and-blinding
author: tutor
date: 2026-08-28
status: draft-for-review
epistemic: protocol specification; zero labels collected
---

# Rater qualification and blinding SOP

Label population is **blocked** pending real qualified human raters plus Peter's
explicit approval. This document specifies how a person becomes a rater and how
every rating is blinded. It collects zero labels.

No AI or model output may qualify as a rater. `RaterQualification.is_machine` is
structurally pinned `False`. A machine-produced class, a judge tuple, or a
verifier vote is not a human rating and must not be ingested as one.

## Calibration quiz

1. Build a held-out quiz set that is **not** part of the gold corpus. Quiz items
   must not share `source_trial_id` with any `GoldItemRef` that will enter
   `freeze_corpus`.
2. Freeze the quiz item set and its scoring key. Record `quiz_digest` as
   `canonical_json_digest` of the ordered quiz item identities plus the scoring-key
   digest. Store `quiz_digest` on the qualification record; do not store quiz
   answers on a rating record.
3. A candidate completes the quiz independently, blinded to the same fields
   withheld from gold ratings (see Blinding below).
4. Record `quiz_items_attempted` and `quiz_items_correct`. Promotion to
   `qualification_status="qualified"` happens **only** on a pass. A pass does not
   populate gold labels.
5. `qualified_at` is required if and only if status is `qualified`. Provisional
   and revoked records must leave `qualified_at` null.

## Independence groups

Assign every rater an `independence_group` before they rate. Typical grouping
keys: same lab or employer, same training cohort, same prior joint labeling
session, or a disclosed advising/reporting relationship.

Readiness counts an item `READY` only when at least three distinct qualified
human `rater_id`s span at least three distinct `independence_group` values.
Raters who share a group are not independent: their errors and ontology
readings are correlated, so three same-group labels do not satisfy
`required_independent_raters=3`. Same-group ratings may still be collected;
they do not unlock item readiness.

## Declared conflicts

Each rater lists `declared_conflicts` before qualification and updates the list
if a conflict appears later. Conflicts include authorship of the source trial,
prior exposure to the unredacted trace, a reporting line to another rater on
the same item, or any financial/professional stake in a model or agent under
study. A disclosed conflict that places two raters in the same dependence
structure must be reflected in `independence_group` (or the rater is withheld
from that item).

## Revocation

Set `qualification_status="revoked"` (and clear `qualified_at`) when any of the
following holds:

- quiz pass cannot be reproduced, or quiz identity does not match `quiz_digest`
- unblinding: the rater saw machine judgments, verifier verdicts, rewards,
  other raters' labels, or model/agent identity
- labeling was machine-assisted or the rater is not a person
- an undeclared conflict is discovered
- the rater is no longer available; their existing `missing_reason` ratings
  never count toward readiness

Revoked and provisional raters do not count toward the three-rater rule.

## Blinding

Raters see the evidence window only: steps
`evidence_start_step` … `evidence_end_step` after redaction. The following are
withheld and must not appear in the rater-facing payload:

- machine judgments and judge tuples
- verifier verdicts and oracle/nop outcomes
- reward values and primary-reward fields
- other raters' labels, notes, and adjudication outcomes
- model identity, agent identity, and adapter/profile names

Every `RatingRecord` asserts `blinded: true`. A record that cannot assert
blinding is not admissible.

### Redaction steps and `redaction_digest`

1. Slice the source trajectory to the inclusive evidence window on `GoldItemRef`.
2. Drop withheld fields listed above. Replace dropped values with a stable
   redaction token so window length remains reconstructible.
3. Drop any residual model/agent identifiers in tool payloads or file names
   that would identify the producer.
4. Canonicalize the redacted window with `canonical_json_digest`. That digest
   is `redaction_digest` on `GoldItemRef`. Changing redaction mints a new
   digest; the underlying `source_sha256` is unchanged.
5. Present only the redacted window to the rater. `evidence_step_ids` on the
   rating record must be a subset of that window.

This SOP does not authorize label collection.
