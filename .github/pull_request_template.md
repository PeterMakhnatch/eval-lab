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
  - Focused: `make prepush TESTS='...'` (`scripts/premerge.sh --focused TEST_FILE`) or `make check` (`scripts/premerge.sh --static`)
  - Or explicit full local reproduction: `make premerge` (`scripts/premerge.sh`)
- [ ] Full CI complete on GitHub Actions (`quality-required`, `typecheck-required`) for this exact head SHA
- [ ] Independent review completed and `independent-review` status attested for this exact head SHA
- [ ] Diff confined to leased paths
- [ ] Handoff / receipt updated with pasted evidence

## Merge

- **Merge owner:** <release owner or assigned merge owner — never the PR author>
- Exact-head rule (`agents/CHECKS.md`): merge only after `gh pr checks <n>`
  shows every reported check successful **for this head SHA**. No local green,
  stale run, or mergeability substitute.
