Status: review-wanted
Last: Lanes/missions governance implemented; fleet-status rewritten with 9/9 injected-output tests; premerge pending
Next: Open PR "M001: simplify mission governance" and STOP — merge belongs to a non-author integrator
Blockers: none

# M001 handoff — simplify mission governance

Lease (exact): `agents/{WORKFLOW,STRUCTURE,ROLES,OWNERS}.md`,
`agents/missions/`, `agents/archive/`, `docs/operating-manual.md`,
`scripts/fleet-status.sh`, `tests/test_fleet_status.py`,
`.github/pull_request_template.md`, this file. Nothing outside it was touched.

## What changed

- `agents/OWNERS.md` — four lanes (Integration, Research, Tasks, Platform)
  with path ownership and decision rights; Peter's reserved authority limited
  to policy/spend, publication, research direction, task registration.
- `agents/missions/ACTIVE.md` — the sole live board. Top page answers
  Now / Review / Next / Needs Peter truthfully as of registration: M001 in
  flight; four legacy in-flight worktrees adopted as LEGACY-* rows
  (program, organization-prompts, wave4-prompts, observatory-b3); no open
  PRs; M002 (ty ratchet 33→28) and M003 (spent-branch triage) queued ready;
  only M001's review needs Peter.
- `agents/missions/TEMPLATE.md` — row template + worker rules.
- `agents/archive/2026-08-15-role-registry.md` — the full 28-role historical
  table preserved verbatim; `agents/ROLES.md` reduced to a compatibility
  pointer.
- `agents/WORKFLOW.md`, `agents/STRUCTURE.md`, `docs/operating-manual.md` —
  ownership references now point at OWNERS/board; STRUCTURE change log entry
  added; zero `ROLES.md`-as-authority references remain in WORKFLOW/manual.
- `scripts/fleet-status.sh` — rewritten. Derives: board headings, branch
  liveness (SPENT via 0-ahead, tree-identical-to-main, or merged-PR head),
  UNREGISTERED (active branch absent from board), STALE (active with no
  commit in FLEET_STALE_HOURS), board hygiene (board rows whose branch is
  gone), handoff headers, open PRs, queue, digest. All externals behind
  FLEET_GIT / FLEET_GH / FLEET_ROOT seams; gh absence degrades gracefully.
- `tests/test_fleet_status.py` — 9 tests, injected git/gh executables and a
  fixture board; no host branches, no network, no gh auth.
- `.github/pull_request_template.md` — mission/lane, provenance, lease,
  deps, acceptance, merge owner, exact-head check rule.

## Evidence

```
$ uv run pytest tests/test_fleet_status.py -q
.........                                                                [100%]
9 passed
```

(One fixture bug found during writing — the hygiene test originally asserted
against a branch the fixture never made missing; fixed in the fixture, the
script was right.)

Premerge, honest sequence: the FIRST full run FAILED —
`test_repository_contract.py::test_fleet_status_reads_rotated_event_segments`
requires fleet-status to read rotated `queue/events.jsonl.*` segments, which
the rewrite had dropped. The contract test is outside this mission's lease,
so the script was restored to conform (events section re-added). Final run:

```
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28
```

(ty baseline is already 28 on main — M002's premise is half-done upstream;
integrator should reconcile the M002 row.) The new fleet-status also
smoke-ran read-only against the real repo and correctly rendered the board
and branch states.

## For the integrator

- Real-repo observations the new fleet-status will surface immediately:
  `role/data-strategy` (+10), `role/solidify` (+53) and friends are
  squash-spent but look active under the old script; three genuinely live
  worktrees (organization-prompts — commits 5 min before M001 registration,
  program, wave4-prompts) were adopted onto the board rather than left
  unregistered.
- M001 does not delete any branch or worktree (mission constraint); M003
  covers triage.
