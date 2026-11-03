---
status: living
audience:
  - operator
---

# Independent PR review

Use a different model family to look for concrete bugs before merging agent work:

```bash
uv run python scripts/review_pr.py 1234 --contract /tmp/change-contract.md
uv run python scripts/review_pr.py 1234 --head <40-hex sha>    # pin the review to one commit
uv run python scripts/review_pr.py 1234 --verify-fixes /private/tmp/previous/REVIEW.md
uv run python scripts/review_pr.py 1234 --check /private/tmp/previous/receipt.json   # merge-time rule
uv run python scripts/review_pr.py 1234 --mode shallow    # force the cheap static review
```

The default is `google-antigravity/gemini-4-argon`, using the operator's existing
Gemini subscription login. `--model PROVIDER/MODEL` selects another exact route;
operators remain responsible for any required spend approval. No fallback is
configured, inherited API-key variables are removed, and the served model is
checked afterward. These precautions do not authorize paid models or establish
that an arbitrary alternate route is subscription-funded.

## Two modes

The command pins one commit (the PR's head when it starts, or `--head SHA`, which must be that head
or one of its ancestors) and the PR's base, fetches the PR, and creates a
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
  `--lanes behaviour,adversarial` runs only a subset (useful on a loaded machine); the consolidated
  report then says which lanes were not run and `Coverage: PARTIAL`, and coverage counts only the
  requested lanes. Lane names must be distinct and valid; `--lanes` is ignored in shallow mode.

Deep reviewers execute real commands in their own worktree, scratch directory and
`TMPDIR`. This is a prompt- and tool-limited review, **not an OS security sandbox**: a reviewer
could in principle touch files outside its directories. A reviewer that leaves its checkout dirty
or commits into it loses that lane's verdict (see below).

## Report and verdict

Each reviewer writes a `REVIEW.md` with `## Blocking`, `## Should-fix`, `## Nit` and a final
`VERDICT: PASS` or `VERDICT: BLOCK`; PASS is rejected when the blocking section is not `None`.
The format is strict and narrow: the verdict is only the last non-empty line, exact and unindented
and outside code fences (a second exact verdict line is malformed); each required heading appears
exactly once, in exact ASCII and order, and a level 1-2 heading that merely looks like one
(case, zero-width or look-alike letters) is malformed; fences follow CommonMark. Everything else
is ordinary text, so findings may say `Expected: BLOCK` or `Verdict: passing in CI`, use em dashes
in titles, and nest code fences in lists. Anything malformed is never a PASS.
In deep mode the tool consolidates the lane reports into `REVIEW.md` in the evidence directory,
**always**, even when a lane timed out or crashed:

- a lane counts only if it exited normally, was attested, left its checkout clean and wrote a valid
  report; anything else is `COVERAGE INCOMPLETE` and its text is kept only as quoted, unvalidated
  notes;
- **PASS** needs every lane complete and clean; partial coverage is never PASS;
- **BLOCK** when a complete lane reports a blocking finding;
- **INCONCLUSIVE** when coverage is partial and no complete lane found a blocker. It is **never a
  valid result**: the receipt has `valid: false`, no verdict, `outcome: "INCONCLUSIVE"` and an
  error naming the incomplete lanes; `REVIEW.md` (kept for diagnosis, ending in `VERDICT: BLOCK`)
  starts with the `REFUSED` notice, and the command exits 3.

A BLOCK or PASS that rests on partial coverage (a `--lanes` subset, or a BLOCK found while another
lane timed out) is still a valid receipt, but its `REVIEW.md` says `Coverage: PARTIAL`, so it is
not a `--verify-fixes` base and `--check` rejects it.

A reviewer does not replace executable tests or exact-head CI.

## A verdict covers one commit

`REVIEW.md` starts with a quoted block naming the reviewed head (`> Reviewed head: <sha>`), and
`receipt.json` records `head_sha`, `pr_head_at_start`, `pr_head_after` and `head_moved`. A PR that
advances during or after the review does not invalidate the verdict, but the verdict does **not**
extend to the new commits, and no heuristic ever decides that a delta is unrelated.

Merge-time rule: run `--check RECEIPT.json` against the PR. It does **not** trust the receipt's
fields. It first re-derives the result from the evidence directory next to the receipt (the
reports and session files on disk, their hashes, served-model attestation, full lane coverage,
`REVIEW.md` binding and verdict); any inconsistency exits 2 and prints the problems, with
`covers: false`. Only then does it compare the verified commit with the PR's current head via
`gh`: equal exits 0 (PASS) or 1 (BLOCK); different exits 4, with the `--verify-fixes` hint. A
hand-written five-field receipt, an edited `REVIEW.md` or session file, a partial-coverage or
inconclusive review, or a receipt for another PR is exit 2.

Threat model: this catches stale, accidental and inconsistent results. It does **not** stop a
deliberate forger who can write the evidence directory (they can rewrite the evidence and the
receipt together). The gate is only meaningful when the coordinator runs the review itself and
keeps the evidence outside the PR author's reach.

On 4, run a delta review: `--verify-fixes PREV_REVIEW.md`. The previous report must be a valid
review (it must parse), belong to this PR, be a complete-coverage verdict and not be refused;
otherwise the run is refused. Its bound commit gives the delta (`git diff PREV HEAD`); the
reviewer re-checks each earlier finding and examines only the new commits, and the mode is chosen
from the size of that delta, so it is small and fast. A valid previous report that is unbound
(older, hand-written) or whose commit is not an ancestor of the new head falls back to a full
review (recorded in the receipt `notes`); reviewing the exact commit that was already reviewed is
rejected.

## Attestation and refusal

Every session JSONL of every reviewer is hashed and checked: assistant messages are counted per
provider/model, `resolvedModelIsFallback` flags and model changes are recorded. Any off-route
model or fallback in any reviewer refuses the whole run: no verdict, no comment. Missing or
malformed evidence in a lane only marks that lane incomplete.

After each reviewer finishes (also after a timeout) the tool inspects its checkout. Dirt
(`git status --porcelain --untracked-files=all` not empty) is written to
`<lane directory>/dirty.txt`, and a `HEAD` different from the reviewed commit (the reviewer ran
`git commit`) is also a failure: either way that lane is **incomplete**, whatever its report
says, and the other lanes keep their verdicts. In every case `REVIEW.md` and the lane reports stay
for diagnosis but are not an accepted verdict: whenever the final receipt is not valid, `REVIEW.md`
is prefixed with a `REFUSED: no valid verdict was issued: <errors>` notice and a trailing
`VERDICT: PASS` is rewritten to `VERDICT: BLOCK`, so it cannot be mistaken for a pass or used as
a `--verify-fixes` base. Reviewer processes are killed as a process group when they finish or
time out.

## Evidence, comments, cleanup

Evidence is retained outside Git: `brief.md`, `pr.diff`, `REVIEW.md`, `omp.log`, `sessions/`
(per reviewer under `reviewers/<lane>/` in deep mode) and `receipt.json`. `--out DIRECTORY`
chooses a new evidence directory (existing ones are refused); by default one is created under
`/private/tmp`, so copy important evidence to private durable storage.

Every owned worktree is removed on success or failure with `git worktree remove --force`. This is
safe because a dirty or moved checkout was already recorded (`dirty.txt`, above) and its lane is
incomplete; without `--force` real git fails on a dirty tree and one dirty lane would invalidate
every other lane's verdict. Only a failed forced removal keeps the path and invalidates the
receipt.

Nothing is posted by default. `--comment` posts a bounded blocking summary only for a validated
PASS or BLOCK, after cleanup; the summary quotes the parsed `## Blocking` section and names the
reviewed commit. The report must contain no secrets,
third-party transcripts, or per-row third-party identifiers. This advisory creates no commit status,
changes no branch protection, merges nothing and does not revive the retired `independent-review`
CI gate.

Exit codes of a review: **0** valid PASS; **1** valid BLOCK; **3** INCONCLUSIVE (partial deep
coverage and no blocker found: never a valid result); **2** any other invalid or refused review.
`--check` exits **0**/**1** for a verified PASS/BLOCK of the current head, **4** when the head
differs, **2** otherwise.

Synthetic, model-free tests:

```bash
uv run pytest tests/test_independent_review_contract.py tests/test_review_pr.py
```
