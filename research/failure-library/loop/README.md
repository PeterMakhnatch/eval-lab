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
is deterministic SHA256(`failure-library-canary/v1`, NUL, identity). Each available
strongest-evidence tier is seeded once; remaining slots round-robin over tiers
with unused matched controls before falling back to unmatched tasks. Within a
tier, sources with eligible negatives and digest-bound packages come first.
This preserves tier coverage without arbitrarily sacrificing negative controls.
Existing DEV answer-key controls need a pinned task digest, no known finding on
those task bytes and
an exact source match, preferring the same revision. ImpossibleBench originals
use the source owner's pinned, cached parquet loader and match the same original
problem; their whole-row digest is **not** misrepresented as a task-directory
digest. No download is attempted if the original cache is absent. Duplicate
answer-key labels on identical task bytes cannot inflate the control count.
A source-negative is not a certificate of task correctness; weak-negative and
construction-negative label strength is preserved. An original with a native
baseline-name collision against a non-ImpossibleBench defect claim is withheld,
not declared equivalent across source versions. New conflicting task-byte or
baseline claims also invalidate frozen negatives before any model call.
Missing controls remain a deficit, never a synthetic negative. Runs require
unambiguous **executed or human**
`reward_hacked`/`clean` labels. Reward-only, judge and construction labels cannot
become hack ground truth.

Each selected record and input collection has a digest. Repeat invocations reuse
the same slice rather than quietly rerolling after ingestion. Selected finding,
run or control byte changes, run-manifest mismatches and split drift block model
calls. New independent slices use an explicit new `--manifest` path.

The default native execution subset is two balanced tasks and two balanced runs
per arm. Increase `--max-items` explicitly, up to 40. Both knowledge arms use
exactly the same selected IDs. Seeker execution remains unsupported and disabled.
Packets exclude **all** frozen raw finding/run IDs with `--exclude-id`, and
every frozen native `SOURCE/TASK` (including clean controls) with `--exclude-task`,
plus the compiler's mandatory same-task/family/template exclusions. Synthetic
loop manifest IDs are not passed as library record IDs. Both exclusion sets
must be preserved in the compiler manifest. Missing group authority, an empty
packet or omitted exclusions fail the ON integration rather than pretending
the unchanged baseline received usable knowledge.

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
- **Feedback:** read-only source claims cannot mint V3 evidence. Task-version-bound
  independent support must be reviewed by the owning source builder before
  re-ingestion. This package ships no exploit/re-break verification executor.

`scoreboard.csv` records agent, knowledge ON/OFF, recall, false-alarm count/rate,
metered cost, tokens, wall time and review denominators. Missing measurements are
blank, never zeros or passes. Recalls are over completed reviews with positive
labels; false alarms are over completed source-negative controls. The same
heartbeat retains review coverage and selection deficits, so successful-looking
conditional rates cannot conceal missing stages. `knowledge_effect` computes
ON-minus-OFF recall/false-alarm deltas only over identical subjects completed
in both arms; missing paired positive or control coverage blocks a QA improvement
claim. Seeker targets only broken tasks, so its paired metric is recall and
false alarms remain unmeasured. The shared `qa score` CLI is
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
improved. The native `canary` command enables only subscription/offline stages
and launches no paid API calls or sandboxes.

## Repeatable archived-source diagnostic pilot

`evallab failures loop readonly-pilot` is a separate text-only diagnostic, not
the native Harbor Task/Trace pipeline and never a Reward Seeker runner. It uses
the existing QA `make_driver` factory. Each subject/arm gets one fresh isolated
conversation; tools, rules, skills, extensions, retries and fallback are disabled
by the existing OMP driver. The supported zero-dollar pilot is explicitly pinned
to `meta/muse-wonder-1.0-contributor`; usage remains visible despite zero charge.
The direct OpenAI-compatible lane requires explicit keys, positive prices,
`--allow-paid`, and caps no greater than $0.50 total / $0.05 per request.

The committed `pilot-{task,trace}-20261027.manifest.json` files contain source
records, pins, hashes, exact extraction locators/spans and private document
references, **not raw transcripts**. Task source is the pinned instruction,
grader source and environment Dockerfile. Trace source is a bounded native
Parquet role/content view, not a fabricated Harbor/ATIF trajectory. Only cited
document contents enter the prompt; row grading columns and identity/label
sidecars do not. `extract_pilot_views.py` preserves the offline reconstruction
method, including the package digest versus exact-row/span distinction.

**Source admission is enforced:** the public runner reconstructs the same source
documents before any model request. Task views require the whole pinned package
digest and in-package files. Trace views bind the exact native row, record
identity/outcomes/labels, message spans and declared redaction. A frozen view hash
plus an unrelated genuine source checksum is rejected. Private document-file
checksums and parsed-view equality are both checked. `extract_pilot_views.py`
uses the same production extractor; `--bind` regenerates metadata only after
native reconstruction matches the frozen document and line-cited-view digests.

From the integrated checkout, the owner can validate the frozen inputs offline:

```sh
STATE="${EVALLAB_FAILURE_LIBRARY_STATE:-$HOME/.local/state/evallab-failure-library}"
TASK=research/failure-library/loop/pilot-task-20261027.manifest.json
TRACE=research/failure-library/loop/pilot-trace-20261027.manifest.json
python research/failure-library/loop/extract_pilot_views.py "$TASK"
python research/failure-library/loop/extract_pilot_views.py "$TRACE"
evallab failures loop readonly-pilot --dry-run --lane omp \
  --model meta/muse-wonder-1.0-contributor --manifest "$TASK" \
  --pack-dir "$STATE/integration-smoke-20261028/task-empirical" \
  --output-dir "$STATE/loop/readonly-task-NEXT"
evallab failures loop readonly-pilot --dry-run --lane omp \
  --model meta/muse-wonder-1.0-contributor --manifest "$TRACE" \
  --pack-dir "$STATE/integration-smoke-20261028/trace-curated" \
  --output-dir "$STATE/loop/readonly-trace-NEXT" --allow-curated-fallback
```

A manually authorized route decision may remove `--dry-run`, using **new**
output directories; provider quota failures are preserved and never retried or
rerouted automatically. The empirical Task packet must seal every frozen
record/task exclusion. Curated Trace guidance is labelled
`preexisting_curated_defensive_taxonomy`, never evidence-retrieved knowledge.

The first real public CLI attempt reached Muse on October 27 at about 8:05 PM ET.
Its first Trace OFF request failed with quota HTTP 429 before generation; the
provider reported all-zero usage and a reset at **8:21:23 PM ET**. No retry,
fallback, ON request or Task model request followed. `heartbeat.json` and
`scoreboard.csv` preserve that failure; accuracy and missing wall measurements
are blank, not successes or measured zeros. The original private receipt remains
immutable, including its exception-path wall-time fallback quirk. No comparative
improvement or independent defect confirmation was established.

