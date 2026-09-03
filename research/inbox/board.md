# Board — pull, don't push

## status (keeper-maintained TL;DR for humans)
- Open tops: good-codebase #1 · analysis — none · system-design — none · tooling — unseeded · omp-usability — awaiting Peter seed · training-signal #1/#2
- Active: census (wK:p7, blocked) · gates (wK:p4, blocked) · live-fire (engineer-lead) · C-repair (wS:p2, blocked) · GPU record (Fable) · evidence-visibility (wS:pA)
- Review queue (Peter): verl decision · flake verdict · reward branch · visibility branch · triage branch
- Needs Peter: wK:p7 two yes/no · wK:p4 host call · triage adjudication · tooling seeds

# What lives where
# - problems: raw pains, anyone appends. Peter promotes to backlog/rules.
# - backlog: the menu. Peter orders. Agents pull top-down or pass.
# - claims: who is doing what NOW (pane + model). done: finished + attribution.
# - ledger.md: permanent record + marks. claims/: pickup counter (see README).

# Chains vs cohorts
# - chain: B needs A's output (`chain: after #1`). One active claim per chain.
# - cohort (default): independent items, any order. No relation tracking needed.
# - Lanes are named backlogs with one bar each. Chains stay inside lanes.

# house rules
1. No agent assigns work to another. Propose to the backlog instead. Ever.
2. Only 🟣 Peter pushes. Everyone else pulls (claims/ dir, not pages).
3. Claims sign pane + model, never bare roles.
4. Urgency goes through Peter, never peer-to-peer.
5. One claim at a time. Finish/release before next. Stale 24h → back to board.
6. Passing is allowed and neutral. A reasoned pass is data.
7. Pages: grants, DONEs, blocks, urgency. Nothing else. No claim pages.
8. New rules start as problems, graduate on Peter's promote.

# problems
- P1 (Peter): peer pages out of the blue; OMP unusable. Suspect: peer-first
  delegation defaults. Status: quiet protocol live (claims/ + keeper).
- P2 (Peter): inbox unusable mess. Status: triage branch done, awaiting review.
- P3 (Peter): role names hide WHO. Status: rule 3 live, ledger format fixed.
- P4 (system): pane↔model identity unstable. Status: watching; shas on artifacts.

## good-codebase (bar: CI green + focused tests)
backlog:
  1. immutable_directory nofollow hardening → detail: lead-sync-reply-wH-p1.md
  2. executor hygiene follow-ups → detail: lead-sync-reply-engineer-lead.md
  - proposed by wS:p9: merge 1ec49c13 + allowlist parity lint → evidence: probe-lane3-flake-20260903.md
claims:
done:
  - wS:pA → #2 executor hygiene (21561c98 pushed, green, ruff clean; review: wH:p1)

## analysis (bar: digests + file:line on every claim)
backlog:
  1. reward_info projection gap → DONE (below)
  2. PROBE: lane-3 flake → DONE (below)
claims:
  - wK:p7/Fable → deficit census (BLOCKED: Peter yes/no on fbee62dc + tier)
done:
  - wR:p1 → #1 reward gap DONE (branch @2a9d771f VERIFIED, not pushed; acceptance sha 0de6569b CONFIRMED; exact 1/0/5; pending Peter review)
  - wS:p9 → #2 flake VERDICT harness_failure (sha 08d062f3 CONFIRMED; unmerged 1ec49c13 latent on main; pending Peter review)

## system-design (bar: decision + rejected alternatives + kill gate)
backlog:
  1. PROBE: verl second-wave criteria → DONE (below)
  2. S0 GPU sign-off (24GB declared; needs owner confirm)
  3. evidence-visibility → DONE (below)
claims:
  - wK:p4/Tutor → Track F gates (BLOCKED: Peter host + ICC; wH:p9 control arm)
  - wS:p9 (Fable) → #2 GPU record (granted; record ≠ authorization)
  - wS:pA → #3 evidence-visibility (granted; EXP-S03 de-dup respected)
done:
  - wK:p9 → #1 KEEP REJECTED till 4 conditions (sha f079fb59 CONFIRMED; pending Peter review)
  - wS:pA → #3 visibility DONE (commit 6f465650 VERIFIED, branch pushed; 7 contract tests + consumers green; LIVE 238 = 179 eligible + 59 excluded; pending Peter review + push)

## tooling (bar: in-repo, tested, no new harness deps)
backlog:
  1. (Peter to seed)
claims:
  - engineer-lead → zai-opencode live-fire (k1h dispatched)
done:

## omp-usability (bar: operable half-asleep, 5-line docs)
backlog:
  1. inbox triage → DONE review candidate (below)
  2. (Peter: further gripes)
claims:
done:
  - wH:p1 → #1 triage (branch @7177e1b0, 68 moves, 39-active restore; NOT PUSHED; pending Peter review/integration)

## training-signal (bar: digest-bound manifest, no held-out claims)
backlog:
  1. C×H reconciliation → detail: lead-sync-reply-wSp2.md
  2. S0 dry-plan fixtures (Qwen3-0.6B, no GPU)
claims:
  - wS:p2/Synth → C PR #360 repair (BLOCKED by p7 review)
done:
