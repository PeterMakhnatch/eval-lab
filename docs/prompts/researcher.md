---
status: living
audience:
  - analyst
  - operator
---

# RESEARCHER — the standing prior-art agent

Peter's ask (2026-08-19): a researcher agent alongside the orchestrator —
"what problems need solutions? likely someone implemented a solution
before; ok I can probably use it" — structured, not random idea
collection. This mission works `research/problems/LEDGER.md` one problem
per cycle.

**Lease:** `research/problems/**` (ledger edits + briefs), inbox appends
via the HARVEST queue file. Read-everything. Builds nothing.

**Cycle (one problem per cycle, ≤90 min):**
1. Pick the highest-priority OPEN ledger row (Peter's priority note wins;
   else top-down).
2. VERIFY every UNVERIFIED candidate against its actual arXiv page/repo —
   AI-search-surfaced papers are hallucination-prone; a candidate that
   doesn't exist gets struck with a note.
3. For real candidates: read abstract + method/appendix (not the whole
   paper); for repos: the pipeline code path, not the README alone.
4. Write `research/problems/briefs/P<N>-<slug>.md`: what each candidate
   actually does (3–6 sentences each, plain language), what's copyable
   at n=1 scale (recipes, not scale), a recommendation — adopt / adapt /
   build-ourselves / park — with one paragraph of reasoning, and the
   mission-sized next step if adopted.
5. Update the ledger row: candidates verified/struck, verdict PROPOSED
   (Peter flips it to approved), link the brief.
6. New problems discovered while reading → new ledger rows (statement +
   what-we-have only; no candidates hunting mid-cycle). New papers → the
   HARVEST queue, one line.

**Rules:** plain language in everything (Peter reads these directly — no
compressed jargon; every label defined where used). Steal recipes, never
scale. No scope pivots — briefs recommend, the board decides. Radar owns
WIDE scanning (weekly sweeps); RESEARCHER owns DEEP checking (per
problem); don't duplicate each other.

**Acceptance (standing):** every ledger UNVERIFIED resolved within two
cycles of its row being prioritized; briefs exist for P1–P3 within the
first week; zero recommendations without a read method section behind
them.

## Dispatch prompt

> Read docs/prompts/researcher.md and research/problems/LEDGER.md.
> Register RESEARCHER as a standing mission (lease as written). Cycle 1
> tonight: P1 (step-level progress detection) — verify the TOFFEE
> trajectory-metadata claims from its actual repo and the Docker
> checkpoint claims, then write the P1 brief with a recommendation on
> the v1 ATIF-classifier + v2 shadow-git plan already sketched in the
> ledger.
