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
```

The default is `google-antigravity/gemini-4-argon`, using the operator's existing
Gemini subscription login. `--model PROVIDER/MODEL` selects another exact route;
operators remain responsible for any required spend approval. No fallback is
configured, inherited API-key variables are removed, and the served model is
checked afterward. These precautions do not authorize paid models or establish
that an arbitrary alternate route is subscription-funded.

The command pins the PR's current head and base, fetches the PR, and creates a
detached checkout under `/private/tmp`. It renders repository rules, an optional
contract, and a standard checklist covering approval/spend, fail-closed statuses,
DEV/TEST gates, label and test isolation, meaningful tests, third-party data, and
documentation claims. Prior findings supplied with `--verify-fixes` must each be
re-checked on the new head, not accepted on the author's word.

The reviewer gets read/search tools plus write for the designated external
`REVIEW.md`; it gets no shell, browser, model delegation, or test execution tools.
This is a prompt/tool-limited read-only review, **not an OS security sandbox**.
The report must separate `## Blocking`, `## Should-fix`, and `## Nit`, end with
`VERDICT: PASS` or `VERDICT: BLOCK`, and cite source evidence. PASS is rejected
when the blocking section is not `None`. A reviewer does not replace executable
tests or exact-head CI.

Evidence is retained outside Git: `brief.md`, `pr.diff`, `REVIEW.md`, `omp.log`,
`sessions/`, and `receipt.json`. `--out DIRECTORY` chooses a new evidence directory
(existing directories are refused). By default one is created under
`/private/tmp`; copy important evidence to private durable storage before tmp
cleanup. Session attestation hashes every JSONL file, counts assistant messages
by provider/model and `resolvedModelIsFallback` flags, and records model changes.
Missing/malformed evidence, a different model, fallback, nonzero reviewer exit,
an invalid report, changed PR head, or modified checkout prevents a verdict.
Raw reviewer output may remain for diagnosis but is not an accepted verdict.

The owned worktree is removed on success or failure. If it is dirty, cleanup
refuses force deletion and reports its retained path; preserve and investigate
those bytes rather than deleting unknown work.

Nothing is posted by default. `--comment` explicitly posts a bounded blocking
summary only after validation and a current-head check. The report must contain
no secrets, third-party transcripts, or per-row third-party identifiers. This
optional advisory does not create a commit status, change branch protection,
merge the PR, or revive the retired `independent-review` CI gate.

Exit codes: **0** valid PASS; **1** valid BLOCK; **2** invalid/refused review.
Use `--timeout SECONDS` to bound the reviewer process (default 1800).

Synthetic, model-free tests:

```bash
uv run pytest tests/test_independent_review_contract.py tests/test_review_pr.py
```
