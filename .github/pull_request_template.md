## Mission

- **Mission / lane:** M### — <Integration | Research | Tasks | Platform>
- **Agent / model provenance:** <harness + model that authored this change>
- **Leased paths:** <the exclusive paths from the board row; the diff must stay inside them>
- **Dependencies:** <mission IDs merged first, or none>

## Acceptance

<paste the board row's acceptance list; check off what this PR satisfies>

- [ ] …

## Verification

- [ ] `scripts/premerge.sh` pass (paste the final line)
- [ ] Diff confined to leased paths
- [ ] Handoff updated with pasted evidence

## Merge

- **Merge owner:** <from the board row — never the PR author>
- Exact-head rule (`agents/CHECKS.md`): merge only after `gh pr checks <n>`
  shows every reported check successful **for this head SHA**. No local green,
  stale run, or mergeability substitute.
