# HARVEST intake queue

The single doorway for external source material, per
`docs/prompts/context-supply-program.md` (LOOP-HARVEST). Order is the program's;
one or two items per cycle. New sources discovered mid-program are **appended
here with one line of why** and are not fetched mid-cycle — that scope rule is
what keeps five missions from independently re-fetching the same paper.

States: `done` (landed conformant in `research/inbox/`) · `pending` ·
`peter-assisted` (needs a human export) · `parked` (named, deliberately not
fetched, with its reason).

| # | Item | State | Landed as |
|---|---|---|---|
| 1 | Meta-Task appendix components (arXiv 2607.27929 HTML) — F.1/F.2/F.3, B, D | **done** (cycle 1, 2026-08-19) | `meta-task-F1-instruction-template.md`, `meta-task-F2-spec-design-prompt.md`, `meta-task-F3-trajectory-judge.md`, `meta-task-B-dimensions.md`, `meta-task-D-review-rubric.md` |
| 2 | llm-as-a-verifier repo — README, `criteria/TEMPLATE.md`, `criteria/terminal_bench.md`, pairwise/progress prompt structures | pending | — |
| 3 | SWE-smith (github.com/SWE-bench/SWE-smith) — pipeline stages and validation gates | pending | — |
| 4 | METR task-standard + guidelines (github.com/METR/task-standard) | pending | — |
| 5 | TOFFEE repo — inversion pipeline steps | pending | — |
| 6 | Scaffold-effect paper 2607.22585 — methods section | pending | — |
| 7 | Drive stragglers | peter-assisted | — |

## Notes per item

**1 — Meta-Task appendices (done).** Fetch targets were pinned in
`docs/prompts/context-loops.md` EX-MT. The paper's HTML build renders the
appendix prompt figures as inline SVG with real text nodes, so F.1/F.2/F.3 were
extracted verbatim rather than paraphrased. License is CC BY 4.0, stated in the
HTML, so verbatim quotation with attribution is permitted — recorded in each
note's `license_note`. One honest gap carried into the note: **Appendix D
describes the 19-criterion review protocol and names all 19 criteria, but the
reviewer prompt itself is not published.**

**2 — llm-as-a-verifier.** Also carries a dataset pointer,
`data/terminal_bench_2.1_trajs/` (full TB 2.1 trajectory corpus). That is an
external *dataset*, not a note: it is flagged for LOOP-INGEST/TRAJ and the
fetch ≠ register discipline applies. VERIFIER cycle 1 is blocked on this item.

**7 — Drive stragglers, Peter-assisted.** The four book PDFs in Drive > Books are
**not** harvested wholesale: book-length, low density per page, and copyright.
When a specific chapter becomes load-bearing, Peter exports that chapter's notes
himself. The two mega-dumps (`.Build`, `Notes - Content`) stay archived —
HARVEST takes curated sources, not chat logs.

## Appended mid-program (append below with one line of why)

- 2026-08-19 — `harbor-index.org` baseline corpus (1,476 trials / 82 tasks behind
  the Harbor-Index 1.0 leaderboard, already recorded in
  `docs/research/external-datasets.md` as *reported*, never imported). Why: if
  TRAJ wants a second labeled-outcome trajectory corpus after TB 2.1, this is the
  next one, and it is a dataset fetch rather than a note. Parked until asked.
