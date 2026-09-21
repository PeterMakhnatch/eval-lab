## Mission

- **Mission / lane / issue:** Linear HAR-### or M### — <Integration | Research | Tasks | Platform | Release>
- **Agent / model provenance:** <harness + model that authored this change>
- **Leased paths:** <the exclusive paths for this change; the diff must stay inside them>
- **Dependencies:** <PRs or issue IDs merged first, or none>

## Acceptance

<paste the board row's acceptance list; check off what this PR satisfies>

- [ ] …

## Verification

- [ ] Local checkpoint passed:
  - Focused: `make prepush TESTS='...'` (`scripts/premerge.sh --focused TEST_FILE`)
  - Static-only when no behavior changed: `make check` (`scripts/premerge.sh --static`)
  - Or explicit full local reproduction: `make premerge` (`scripts/premerge.sh`)
- [ ] Full CI complete on GitHub Actions (`quality-required`, `typecheck-required`) for this exact head SHA
- [ ] No unresolved CI failures or known concrete bugs
- [ ] Diff confined to leased paths
- [ ] Handoff / receipt updated with pasted evidence

## Merge

- **Merge owner / DRI:** <author or integrator>
- Exact-head rule (`agents/CHECKS.md`): merge only after `gh pr checks <n>`
  shows every reported CI check successful **for this head SHA**. Retired `independent-review` statuses are not CI gates. No local green, stale run, or mergeability substitute.
- Protected merge execution:
  `gh pr merge <n> --squash --delete-branch --match-head-commit <HEAD_SHA>`
  followed by bounded postmerge proof on `main`.
