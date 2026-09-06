# Analyst — forensic audit of strict historical dry-run

## Exact target

- Candidate worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ ba2ef5c8fb837499cfe05de9c17630109a564a5f`
- Base: `8fa4d4998b298ee4475eb55e30729f3ed8ef60d7`
- Dry-run report: `/tmp/platform-builder-strict-historical-regeneration-dry-run.json`
- Your prior authority inventory: `/tmp/analyst-strict-historical-regeneration-inventory.md`

Read-only. Do not edit/rebase/apply/integrate, run models/controls, or spawn subagents.

## Goal

Independently prove or falsify that the candidate’s actual 170 dispositions and dry-run manifest implement the prior authority inventory without semantic inference. This is a forensic value audit, not a code-style review.

## Required audit

1. Re-run the public dry-run into a new `/tmp` report with exact `--expect-promoted 170 --expect-derivable 130`; compare canonical bytes/digest to the builder report.
2. Decompose exact trial sets and reasons:
   - 170 promoted;
   - 152 events;
   - 130 truth/descriptive;
   - 128 truth+final;
   - 2 truth without final;
   - 40 without truth;
   - 18 without truth/events;
   - zero analysis-ready/admissible.
   Report exact set relationships and any record whose reasons disagree with its source files.
3. Inspect all serialized disposition/record values for forbidden leakage:
   - task/directory/path/name-derived family, construct, seed, cell, arm, dose, representation, platform, isolation, evidence class, opportunity counts;
   - fallback task ID/registry revision;
   - opportunity 0/1 defaults;
   - absence represented as asserted negative.
4. Verify the 61 historical task content digests remain unbound to registry/design cells and are represented as content identity only.
5. For samples from every cohort plus boundary/tamper fixtures, trace every non-null output field to exact input path/field/digest. Unknown fields must be explicitly unavailable/held.
6. Check the 128/2 distinction: the two missing-final-state records must remain descriptive-incomplete/non-loadable, not disappear or become ready.
7. Check manifest identity: input inventory, code/schema version, disposition identities, output plan, and self/content digest are deterministic and complete; no timestamp/order/path-locality contaminates identity.
8. Confirm dry-run produced no historical contract file and no repository mutation.

## Output

Write `/tmp/analyst-strict-historical-dry-run-audit-ba2ef5c8.md` beginning `APPROVE` or `BLOCK`, with:

- exact command/digests/counts;
- cohort/reason matrix;
- non-null field provenance samples;
- forbidden-value scan results;
- exact blocker records if any;
- observed zero-write status.

Page `wH:p9` with verdict/report path. No implementation recommendations unless grounded in an exact failing record/invariant.
