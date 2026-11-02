---
status: living
audience:
  - builder
  - operator
---

# Independent PR review

Use a different model family to look for concrete bugs before merging agent work:

```bash
uv run python scripts/review_pr.py 1234 --contract /tmp/change-contract.md
uv run python scripts/review_pr.py 1234 --verify-fixes /private/tmp/previous/REVIEW.md
uv run python scripts/review_pr.py 1234 --mode shallow    # force the cheap static review
```

The default is `google-antigravity/gemini-4-argon`, using the operator's existing
Gemini subscription login. `--model PROVIDER/MODEL` selects another exact route;
operators remain responsible for any required spend approval. No fallback is
configured, inherited API-key variables are removed, and the served model is
checked afterward. These precautions do not authorize paid models or establish
that an arbitrary alternate route is subscription-funded.

## Two modes

The command pins the PR's current head and base, fetches the PR, and creates a
detached checkout under `/private/tmp` for each reviewer. It renders repository rules, an optional
contract, and a standard checklist covering approval/spend, fail-closed statuses,
DEV/TEST gates, label and test isolation, meaningful tests, third-party data, and
documentation claims. Prior findings supplied with `--verify-fixes` must each be
re-checked on the new head, not accepted on the author's word.

- **shallow**: one static reviewer with read/search/write tools and no shell. Fast and cheap, but it
  only reads code: on its first real run it passed PR #1815, which a deeper manual review of the
  same head blocked four times. Use it only for small changes.
- **deep** (the default when the PR changes more than `--deep-threshold` lines, 300 unless set):
  four focused reviewers run in parallel, each a separate `omp -p` process with its own
  worktree, session directory, scratch directory and time budget. Lanes: `gates` (spend,
  DEV/TEST, label isolation, every input path to each gate), `behaviour` (run the documented
  commands end to end, edge cases, failure paths), `adversarial` (tamper with copies of
  artifacts, feed inputs that must be refused), `tests_docs` (run tests, mutate the code to see
  whether tests notice, docs overclaims, third-party data). Deep reviewers get
  `read,bash,grep,glob,write,edit` only: no `task` or `eval` tool, so no sub-agent can run on another
  provider. `--timeout` is per reviewer (default 1800 s shallow, 2400 s deep).

Deep reviewers execute real commands in their own worktree, scratch directory and
`TMPDIR`. This is a prompt- and tool-limited review, **not an OS security sandbox**: a reviewer
could in principle touch files outside its directories. A reviewer that leaves its checkout dirty
loses its verdict.

## Report and verdict

Each reviewer writes a `REVIEW.md` with `## Blocking`, `## Should-fix`, `## Nit` and a final
`VERDICT: PASS` or `VERDICT: BLOCK`; PASS is rejected when the blocking section is not `None`.
In deep mode the tool consolidates the lane reports into `REVIEW.md` in the evidence directory,
**always**, even when a lane timed out or crashed:

- a lane counts only if it exited normally, was attested, left its checkout clean and wrote a valid
  report; anything else is `COVERAGE INCOMPLETE` and its text is kept only as quoted, unvalidated
  notes;
- **PASS** needs every lane complete and clean; partial coverage is never PASS;
- **BLOCK** when a complete lane reports a blocking finding;
- **INCONCLUSIVE** when coverage is partial and no complete lane found a blocker (the report ends in
  `VERDICT: BLOCK` but the receipt says `INCONCLUSIVE`).

A reviewer does not replace executable tests or exact-head CI.

## Attestation and refusal

Every session JSONL of every reviewer is hashed and checked: assistant messages are counted per
provider/model, `resolvedModelIsFallback` flags and model changes are recorded. Any off-route
model or fallback in any reviewer refuses the whole run: no verdict, no comment. Missing or
malformed evidence in a lane only marks that lane incomplete. A changed PR head or a
modified checkout also prevents a verdict. In every case `REVIEW.md` and the lane reports stay
for diagnosis but are not an accepted verdict; a refused report that ended in `VERDICT: PASS` is
rewritten to start with a `REFUSED` notice and end in `VERDICT: BLOCK`, so it cannot be mistaken
for a pass. Reviewer processes are killed as a process group when they finish or time out.

## Evidence, comments, cleanup

Evidence is retained outside Git: `brief.md`, `pr.diff`, `REVIEW.md`, `omp.log`, `sessions/`
(per reviewer under `reviewers/<lane>/` in deep mode) and `receipt.json`. `--out DIRECTORY`
chooses a new evidence directory (existing ones are refused); by default one is created under
`/private/tmp`, so copy important evidence to private durable storage.

Every owned worktree is removed on success or failure. A dirty one is never force-deleted; the
receipt reports the preserved path.

Nothing is posted by default. `--comment` posts a bounded blocking summary only for a validated
PASS or BLOCK, after cleanup and a current-head check. The report must contain no secrets,
third-party transcripts, or per-row third-party identifiers. This advisory creates no commit status,
changes no branch protection, merges nothing and does not revive the retired `independent-review`
CI gate.

Exit codes: **0** valid PASS; **1** valid BLOCK; **2** invalid/refused review; **3** INCONCLUSIVE
(partial deep coverage without a blocker).

Synthetic, model-free tests:

```bash
uv run pytest tests/test_independent_review_contract.py tests/test_review_pr.py
```
