# M025 LOOP-HARVEST

Status: cycle 1 complete — ready for review
Last: RECHECK found both existing inbox notes nonconformant and fixed them; intake
queue item 1 (Meta-Task appendices F.1/F.2/F.3, B, D) landed verbatim in five
notes; `tests/test_inbox_conformance.py` added and mutation-verified.
Next: queue item 2 (llm-as-a-verifier repo — README, `criteria/TEMPLATE.md`,
`criteria/terminal_bench.md`, pairwise/progress prompt shapes). It unblocks M027
VERIFIER cycle 1, so it is the highest-value next intake.
Blockers: none. Queue item 7 (Drive book chapters) is `peter-assisted` by design,
not blocked.

## RECHECK — both notes failed, which became this cycle's first work

The program doc says to treat the two existing deposits as cycle-1 RECHECK
material and "verify front-matter/provenance conformance and move on." They did
not conform, so per the loop protocol the failure became the work.

| Finding | Evidence | Fix |
|---|---|---|
| `drive-salvage-2026-08-18.md` was **not in the inbox** — it sat in `research/explorations/` | `git log --diff-filter=A -- '*drive-salvage*'` → `ff6f19e` added it at `research/explorations/drive-salvage-2026-08-18.md` | relocated to `research/inbox/` |
| **Neither note had front-matter at all** — both used a prose `Provenance:` line, so `source_url` / `source_type` / `retrieved` / `license_note` / `status` / `feeds` were all absent | the two files at `4582b3e` | conformant front-matter added to both |

Two honest notes on the fix. The relocation was staged with `git mv` and got
absorbed into Peter's commit `4582b3e` (it appears there as a 0-change rename)
because he committed while it was staged — so the move is already on `main` under
his authorship, not this PR's. And a third note, `egs-best-practices.md`, was
deposited by the operator session mid-cycle; it is **already conformant** and this
mission did not touch it.

`feeds` for the salvage note is deliberately `parked`, with the reason recorded
inline: it is a link-triage note whose value is the queue entries it produced
(items 2 and 6), not a corpus target of its own.

## EXTEND — queue item 1 landed verbatim, not summarized

Fetch targets were pinned in `docs/prompts/context-loops.md` EX-MT. Source:
arXiv:2607.27929v1, *Meta-Task: Turning Terminal Task Synthesis into a Terminal
Task for Scalable Agent Training* (Pan et al., 2026-07-30).

| Note | Appendix ref | Bytes |
|---|---|---|
| `meta-task-F1-instruction-template.md` | F.1 / Figure 6 — synthesis instruction, fixed parts | 7,748 |
| `meta-task-F2-spec-design-prompt.md` | F.2 / Figure 7 — multi-phase spec design prompt | 2,874 |
| `meta-task-F3-trajectory-judge.md` | F.3 / Figure 8 — KEEP/DISCARD trajectory judge | 3,623 |
| `meta-task-B-dimensions.md` | B — 39 categories, 10 scenarios, 4 difficulty levels, plus one representative spec each | 8,340 |
| `meta-task-D-review-rubric.md` | D — the 19-criterion review protocol | 3,451 |

**How the prompt text was obtained, since this is the part that could have been
faked.** The appendix prompts are figures, not body text. In the arXiv HTML build
they are inline `<svg>` with real `<foreignobject>` text nodes, so the text was
extracted programmatically from that markup — not retyped, not reconstructed, and
not paraphrased from the surrounding prose. Verified by string-matching landed
notes against the downloaded source HTML:

```
"Self-Validation"        -> notes:2 source:1
"reward.txt"             -> notes:1 source:1
"Do not imitate the example" -> notes:1 source:1
"=== CATEGORY ==="       -> notes:1 source:1
"Shortcutting"           -> notes:1 source:1
"anti_cheat_robustness"  -> notes:2 source:1
```

Only two mechanical cleanups were applied, neither touching content: LaTeXML list
markers that the extractor emitted on their own lines were rejoined to their item
text (`- •` → `- `), and MathML's duplicated unicode+LaTeX pairs were collapsed
(`↔\leftrightarrow` → `↔`). Residual LaTeX token count after cleanup: 0.

**Licence.** The HTML states `License: CC BY 4.0`, so verbatim quotation with
attribution is permitted. Recorded in every note's `license_note` rather than
assumed.

**The honest gap, carried into the note instead of papered over.** Appendix D
describes the review protocol and names all 19 criteria, but **the reviewer prompt
itself is not published**. `context-loops.md` predicted this ("prompt not
published — note that honestly"); confirmed. Any "their review prompt" we ever
ship would be our reconstruction and must say so.

Also landed: `research/inbox/QUEUE.md`, the queue file the loop-done acceptance
requires ("zero unfetched items remain in the queue file"), with all seven items,
their states, and the mid-program append rule.

## HARDEN — `tests/test_inbox_conformance.py`, proven to bite

Enforces the spec's note format: the six required fields present and non-empty,
`source_type` in {paper, repo, thread, drive, blog}, `status` in
{raw, distilled, superseded}, `retrieved` parseable as an ISO date, non-empty
body, and `feeds` a non-empty list whose every entry either points under
`library/curated/standards/` (or `_proposed_templates/`) or is exactly `parked`.
Reuses `evallab.contextpack.parse_front_matter` rather than adding a second
front-matter convention. `QUEUE.md` and `README.md` are exempt by name — the
queue is not a note.

Mutation evidence (three separate defects, each restored to green):

```
strip license_note from F1
  FAILED test_note_front_matter_is_conformant[meta-task-F1-instruction-template.md]
    - missing or empty front-matter fields: ['license_note']

point B-dimensions' feeds at somewhere/else.md
  FAILED test_note_feeds_names_a_standards_target_or_parked[meta-task-B-dimensions.md]
    - feeds entry 'somewhere/else.md' is neither 'parked' nor a standards target

set retrieved: last Tuesday on D-review-rubric
  FAILED test_note_front_matter_is_conformant[meta-task-D-review-rubric.md]
    - retrieved 'last Tuesday' is not an ISO date (YYYY-MM-DD)

restored -> 17 passed
```

## Observation for STANDARDS and for the spec, not silently acted on

`egs-best-practices.md` sets `source_url: synthesis of arXiv 2607.27929 (Meta-Task),
2607.06233 (TOFFEE), SWE-smith, llm-as-a-verifier, AlphaEvolve blog` — a synthesis
note has no single source URL. The conformance test accepts it, because the spec
defines `source_url` without requiring a URL shape. Tightening the test to demand
one would have broken a note deposited by the operator mid-cycle, so it was left
alone and raised here instead: if multi-source synthesis notes are a first-class
kind, they may want their own `source_type: synthesis` and a `sources:` list. That
is a spec change, which is Peter-level direction.

## Cycle-1 acceptance against the loop-done list

| Loop-done clause | State |
|---|---|
| intake queue items 1–6 landed conformant | 1 of 6 (this cycle's scope) |
| conformance test green in CI | added; green locally, CI on the PR |
| every note's `feeds` names a STANDARDS target or explicitly `parked` | yes, enforced by test |
| zero unfetched items remain in the queue file | queue file created; 5 pending, 1 peter-assisted |
| new-source suggestions appended to the queue rather than chased | one appended (harbor-index baseline corpus), not fetched |
