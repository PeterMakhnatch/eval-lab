# HAR-120/G2 data batch: spec generator (gate G2 of HAR-126)

Part of [HAR-126](../../..). This prep stages the tasks and the spec
generator. The paid runs (30 tasks x 2 attempts = 60 trials, $9 G2 cap
across Modal + Daytona) are launched by the parent after G0 (lf2 frozen),
G1 (eval list frozen) and the PREREG merge. **Nothing here spends
anything: no trial launches, no deploys, no model calls.**

- `make_specs.py` — drops proposal tasks that share a repository with the
  frozen eval list, stages task bytes into the gitignored `tasks/` dir,
  and writes 2 ExperimentSpecs per kept task. `--check` revalidates
  without writing.
- `tasks/` (gitignored) — staged task bytes, reproducible from the
  proposal CSV + variant records.
- `specs/` (gitignored) — generated specs; submit parks them in
  `queue/waiting/`.

## 1. Proposal table (30 tasks from the ledger)

Source: `research/experiments/python-task-ledger/har120_proposal.csv`
(17 original, 12 leak-closed, 1 repair). `run_digest` is the digest to run.

| task | project | image MiB | run | run_digest |
|---|---|---|---|---|
| 001647 | environ | 363 | leak-closed | `sha256:e435a9443935…` |
| 000803 | mdutils | 386 | leak-closed | `sha256:ac99a511015a…` |
| 001870 | markdownify | 386 | leak-closed | `sha256:be5cf80bcb8f…` |
| 000341 | vyper | 396 | original | `sha256:303037d50d00…` |
| 001897 | linkpreview | 397 | original | `sha256:e9f28b6db44e…` |
| 002938 | krakenex | 398 | original | `sha256:15d984ef5103…` |
| 001710 | pbxproj | 402 | original | `sha256:361303219639…` |
| 001399 | django_filters | 403 | leak-closed | `sha256:f8ea9d7813eb…` |
| 002680 | sc3 | 403 | original | `sha256:205818a003ea…` |
| 001661 | friends | 404 | original | `sha256:7281bec02447…` |
| 000813 | django_recaptcha | 406 | original | `sha256:de29f74a1355…` |
| 000838 | ntfy | 407 | leak-closed | `sha256:92b291408586…` |
| 001373 | google | 409 | original | `sha256:2b1c35d2cdba…` |
| 001609 | fabulous | 410 | original | `sha256:e7520a03f564…` |
| 002356 | black | 410 | original | `sha256:3b8144ab8bd4…` |
| 001265 | pelican | 412 | leak-closed | `sha256:3cdbc249d78b…` |
| 000552 | nse | 414 | original | `sha256:d2cbd3f557c5…` |
| 000865 | logstash | 414 | original | `sha256:665cab7d069e…` |
| 002555 | webpush | 414 | original | `sha256:f74d636a79af…` |
| 002552 | miio | 419 | original | `sha256:e9273f225478…` |
| 001865 | DnD_battler | 423 | original | `sha256:dff2af0db18a…` |
| 001269 | responses | 424 | leak-closed | `sha256:541d416818c7…` |
| 000227 | github.com/devpi/devpi | 425 | leak-closed | `sha256:611f1bc3e8df…` |
| 000666 | pyromat | 425 | original | `sha256:76498a2dd70e…` |
| 001618 | github.com/jazzband/pip-tools | 427 | repair | `sha256:6711d55bc4e5…` |
| 002416 | ropetest | 427 | leak-closed | `sha256:2e62717d723a…` |
| 000941 | pooch | 433 | leak-closed | `sha256:1c86f4bd85e9…` |
| 001820 | github.com/lovasoa/marshmallow_dataclass | 434 | leak-closed | `sha256:9a30a95b991f…` |
| 002104 | ollama | 435 | original | `sha256:a23cba57908b…` |
| 002393 | wheel | 435 | leak-closed | `sha256:f5b14f3b6b63…` |

(Full 64-hex digests in the CSV. Variant records live at
`library/task-variants/mimo-v2.6-rl__<task>/<digest12>.json`; the repair
record for 001618 is `6711d55bc4e5.json`.)

## 2. Drop logic (contamination vs the G1 eval set)

A proposal task is **dropped** iff its normalised repository equals any
eval-list task's normalised repository, where the normalisation is the
ledger's own `repo_key`: last path segment, lowercased, `-` and `_`
treated the same — so `github.com/psf/black` and `black` count as one
repository. A project that is just the task id (unknown repository)
never matches. Every drop is printed and recorded in `cohort.json`
(`dropped`: task, project, repo_key, and the eval task that forced it).

The eval list is Data's frozen `research/experiments/ovn-sft-v0/eval_tasks.csv`
(columns task, digest, repo, image MiB). The loader also accepts the repo
column as `project`/`project_key` and the task column as `task_id`, and a
header-only (empty) list drops nothing. `tests/test_har120_make_specs.py`
pins this behaviour; §4 proves it end to end on an overlapping dummy list.

## 3. Generating the specs

```bash
uv run --no-sync python research/experiments/har120-data-batch/make_specs.py \
  --tree <LF2_TREE> --tree-digest <LF2_DIGEST> \
  --eval-list research/experiments/ovn-sft-v0/eval_tasks.csv \
  [--out-dir research/experiments/har120-data-batch/specs]
```

- `<LF2_TREE>` / `<LF2_DIGEST>` come from the G0 worker (lf2 frozen tree
  + digest); the tree is validated with `load_harness_tree` (pin +
  harness validity) and stored repo-relative.
- Route, environment and per-trial limits come from the retained HAR-110
  base spec (`base-specs/student-terminus2-selfhosted-python.json`),
  including `override_storage_mb`, the 120-call / 2.5M-token limits and
  `est_cost_usd`. Spec names are `har120-<task>-a1` / `har120-<task>-a2`,
  `question_ref` `har120-data-batch`, attempts/concurrency 1.
- Staging: originals are copied from the pinned read-only snapshot
  (`derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks`,
  falling back to the primary checkout's copy), leak-closed/repair sides
  are rebuilt with `materialize` (proves the record invariant) and
  copied. Copies go through a temp dir + rename, are owner-writable per
  the task-variants convention, and every staged side is asserted against
  the proposal `run_digest`; the run refuses on drift. Staging is
  idempotent: verified bytes are swapped into place, never mutated.
- Validate without submitting (submit has no dry-run; this reuses its
  exact admission checks — `read_spec`, digest-vs-disk,
  `load_harness_tree`):
  `make_specs.py --check <specs-dir>` (needs the same tree + eval-list args).
- Do NOT submit to the queue from this prep: submission parks specs in
  `queue/waiting/` for the parent's launch.

## 4. Proof (placeholder tree + dummy eval list, $0)

Generated with the HAR-116 loopfix tree
(`research/experiments/har116-loopfix-leak/harness-loopfix`,
`sha256:06e5712c…`) as a stand-in for lf2 and a header-only dummy eval
list into `/tmp/har120-proof`, then `--check` (nothing submitted):

```
proposal: 30 tasks; kept 30, dropped 0
drop check: 0 of the proposal tasks share a repository with the eval list
wrote 60 specs + cohort.json to /tmp/har120-proof
route terminus-2 + selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on daytona; per-trial est $1.85; total est $111.00
check ok: 60 specs + cohort.json in /tmp/har120-proof
route terminus-2 + selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on daytona; per-trial est $1.85; total est $111.00
```

Drop proof on a dummy eval list overlapping `github.com/psf/black` and
`RESPONSES` (case/prefix-insensitive match):

```
proposal: 30 tasks; kept 28, dropped 2
drop check: 2 proposal task(s) share a repository with the eval list:
  - format-code-task-002356 (black): shares repository 'black' with eval task 'format-code-task-009991'
  - format-code-task-001269 (responses): shares repository 'responses' with eval task 'format-code-task-009992'
wrote 56 specs + cohort.json to /tmp/har120-proof-drop
...
check ok: 56 specs + cohort.json in /tmp/har120-proof-drop
```

## 5. Cost estimate (60 runs on lf2 vs the $9 G2 cap)

Inputs: HAR-116 actuals — round 2 ran 20 trials in parallel over 37 min
of Modal wall; $3.21 total Modal billed across both rounds
(`evallab spend day --date 2026-10-01`); Daytona at $0.23094/h on
415 trial-minutes for 40 runs (~10.4 min/run). Server rate $2.8149/h.

| line | math | nominal |
|---|---|---|
| Modal server (1 container, `max_containers=1`) | 3 waves x ~37 min wall x $2.8149/h | ~$5.2 |
| Modal warm (cold start + scaledown-window tail) | 1 warm if waves run back-to-back | ~$0.5 |
| Daytona sandboxes | 60 runs x ~10.4 min x $0.23094/h | ~$2.4 |
| **Total nominal** | | **~$8.1** |

That FITS the $9 cap nominally, with ~$0.9 of margin — and the error bars
point both ways: the lf2 loop fix should cut runaway-loop walls (HAR-116
loopfix-r2 saved 26% of input tokens with 5/10 passes vs 0/10), while
tail walls under contention push up. Auto-stop on the single Modal
session is load-bearing: without it an idle server burns $2.81/h.

Parallelism plan:

1. **Run at 20-way, in 3 waves of 20.** 20 parallel is proven (HAR-116
   round 2: 20 trials, 37 min wall).

2. **Do NOT go to 30.** The Modal app runs a single container
   (`tools/modal-mimo-serve/serve.py`: `max_containers=1`), and SGLang
   captures decode CUDA graphs only to batch 16
   (`--cuda-graph-max-bs-decode 16`). 30 concurrent long-context trials
   would queue past the capture batch into unmeasured slowdown; the
   proven maxima are 14 (HAR-110 v2-r2) and 20 (HAR-116 r2).
3. **Pre-launch check before every wave** (per the HAR-122 proposal and
   the overnight rules): `evallab spend day --date <today-UTC>`, and do
   not launch if committed (settled + in-flight + wave estimate) would
   cross the $9 G2 cap or the $30 overnight envelope. Reconcile both
   bills (`evallab modal billing-reconcile` + Daytona org billing)
   after wave 1, then size waves 2–3.
4. **Queue arithmetic:** 60 specs x $1.85 est = $111.00 if admitted at
   once, against the $20/day standing ceiling — so submit/approve in
   waves (<= 10 specs ≈ $18.50 per the HAR-116 precedent) unless Peter
   raises the ceiling. Per-job $1.85 is fine.

## 6. GEPA reuse (seed evaluations via `prior_run_reference`)

Per Research-Harbor (HAR-126, 04:21Z): G2's rollouts double as GEPA seed
evaluations, the way HAR-110 reused HAR-104's trials. No GEPA campaign is
built here; this is the attach recipe for whoever builds one.

- Everything the evaluator needs is kept: the runner's standard job
  layout (job dir `har120-<short>-a<N>/`, finished trial subdirs
  `<job>__<trial>/` with trial-level `result.json`, published by
  `process-job` to the results home), and the exact task bytes digest —
  every spec carries `task_package_digest`, and `cohort.json` records the
  package + verifier digests per kept task.
- Digest match is by construction: the campaign stages the same proposal
  `run_digest` bytes (originals from the pinned snapshot, variants via
  `materialize` of the same records), so `task_directory_digest` of the
  campaign's staged dir equals the spec's `task_package_digest`, which is
  what `validate_prior_run_reference`
  (`src/evallab/gepa_optimizer/feedback.py`) compares.
- Attach follows `har110-python-gepa/fill_refs.py`: stage the chosen
  finished trial subdirs under the campaign's gitignored `prior-trials/`
  (repo-jailed, no symlinks — the validator refuses otherwise), then
  record `{trial_path, result_sha256}` of the trial-level `result.json`
  plus `task_package_digest` per example.
- One difference from HAR-104: G2 runs **2 attempts per task**, so a
  fill-style "exactly one finished trial" lookup refuses as ambiguous.
  Pick one trial per task first (e.g. the `counted_pass` trial Traces
  marks clean, else the first finished), then attach.
