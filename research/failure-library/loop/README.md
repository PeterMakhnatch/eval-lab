# Failure Library heartbeat

Run manually from an Eval Lab worktree:

```sh
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run evallab failures loop canary --dry-run
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run evallab failures loop canary
```

`--dry-run` is offline and write-free: it selects or loads the frozen slice,
checks its provenance and prints the bounded execution plan. The real command
writes `slice.json` once, updates `heartbeat.json` and appends six rows to
`scoreboard.csv` without discarding prior runs. Each run retains its scores locally.
Exit 1 means a real stage failed or could not run; it does **not** mean a clean
canary. Exit 2 is invalid input or setup. No scheduler is installed.

The initial slice requests 20 broken task versions, 20 matched source-negative
controls and 40 runs (20 hacked, 20 clean). Only DEV enters. A bare task name
appearing on **any** non-DEV answer-key assignment is vetoed across keys. Selection
is deterministic SHA256(`failure-library-canary/v1`, NUL, identity), round-robin
across the strongest evidence tier of each unique task version; within each
tier it prefers sources with available negative controls. Existing DEV answer-key
controls need a pinned task digest, no known finding on those task bytes and
an exact source match, preferring the same revision. ImpossibleBench originals
use the source owner's pinned, cached parquet loader and match the same original
problem; their whole-row digest is **not** misrepresented as a task-directory
digest. No download is attempted if the original cache is absent. Duplicate
answer-key labels on identical task bytes cannot inflate the control count.
A source-negative is not a certificate of task correctness; weak-negative and
construction-negative label strength is preserved. Missing controls remain a
deficit, never a synthetic negative. Runs require unambiguous **executed or human**
`reward_hacked`/`clean` labels. Reward-only, judge and construction labels cannot
become hack ground truth.

Each selected record and input collection has a digest. Repeat invocations reuse
the same slice rather than quietly rerolling after ingestion. Selected finding,
run or control byte changes, run-manifest mismatches and split drift block model
calls. New independent slices use an explicit new `--manifest` path.

The default actual execution subset is two balanced items per agent/arm; increase
`--max-items` explicitly, up to 40. Both knowledge arms use the same selected IDs.
Packets exclude **all** frozen finding/run IDs, plus the compiler's mandatory
same-task/family/template exclusions. A missing or empty packet is a failed ON
integration, never evidence for a knowledge comparison.

## Consumer and score boundaries

- **Task QA:** existing `qa task --stages S1` is text-only subscription review;
  ON supplies the compiler's `rubric.toml`. S0/S2/S3 and cloud backends never run.
  A completed three-review S1 majority flag is an **advisory** target-quality
  signal. It is not an executed or independently verified finding.
- **Trace QA:** native Harbor trials enter through `qa trace bundle`, which uses
  `blind_atif`, then subscription `qa trace review --stages T0,T1`. Source-native
  row/field transcript references require a provenance-preserving exporter;
  the loop will not invent trajectories. The current owner-selected library
  prior/disable-prior CLI seam is missing, so ON is reported as failed.
- **Reward Seeker:** its current `--hackbook` is a boolean selecting an internally
  generated global toolkit, not an external library packet. A bounded defensive
  review mode and an owner-selected packet interface are requested from its
  owner. The heartbeat does not generate or execute exploit payloads, run its
  stale approval's replay command, or call submission syntax validity success.
- **Feedback:** read-only source claims cannot mint V3 evidence. Confirmation
  belongs to `failures verify run` with owner-approved target material and spend;
  identity-checked confirmed receipts then re-enter through owning source builders.

`scoreboard.csv` records agent, knowledge ON/OFF, recall, false-alarm count/rate,
metered cost, tokens, wall time and review denominators. Missing measurements are
blank, never zeros or passes. Recalls are over completed reviews with positive
labels; false alarms are over completed source-negative controls. The same
heartbeat retains review coverage and selection deficits, so successful-looking
conditional rates cannot conceal missing stages. `knowledge_effect` computes
ON-minus-OFF recall/false-alarm deltas only over identical subjects completed
in both arms; missing paired positive or control coverage blocks an improvement
claim. The shared `qa score` CLI is
also exercised on the frozen task-label snapshot, keeping native
`qa.finding/v1` scores separate from S1 advisory signals; no pass finding is
fabricated to fill a denominator. This is a DEV engineering heartbeat, not an
unseen-set capability claim.

Private stdout, stderr, packets, command receipts and per-item results live in
`$EVALLAB_FAILURE_LIBRARY_STATE/loop/<run-id>/` (normally
`~/.local/state/evallab-failure-library/loop/`). Native Task QA artifacts stay under
the owned worktree's ignored `runs/.failure-library-loop/`. Git contains only the
small frozen manifest, heartbeat and scoreboard, never raw transcripts. Goodfire
and HackBench data are not uploaded to Laminar or Docent.

Cross-lane integration requests are recorded in the mission bridge's
`2026-10-27-failure-library/requests.md`. The first real receipt must be read as
its measured result—including failure—not as proof that all three agents have
improved. Subscription stages and offline commands are the only enabled routes;
this command launches no paid API calls or sandboxes.
