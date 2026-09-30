# HAR-116: loop-fix test + PyPI leak study (task + spec prep)

Part of [HAR-116](../..). This prep worker stages the tasks and the spec
generator. The paid runs (30 trials, $5 total across Modal + Daytona) are
launched by the parent after 00:00Z. **Nothing here spends anything: no
trial launches, no deploys, no model calls.**

- `tasks.json` — the committed task manifest (single source of truth for
  sides and digests).
- `make_specs.py` — stages task bytes into the gitignored `tasks/` dir and
  writes the 30 ExperimentSpecs. `--check` revalidates without writing.
- `tasks/` (gitignored) — staged task bytes, reproducible from the manifest.
- `specs/` (gitignored) — generated specs; submit parks them in
  `queue/waiting/`.

## 1. Part A: loop-fix arms (10 tasks x {baseline, loopfix} = 20 specs)

The 10 HAR-110 v2 tasks. Both arms run the SAME task bytes: the HAR-113
leak-closed variant where one exists, else the original. Only 002256 and
002864 of the 10 have leak variants (`leak_variants.json` has no entry for
the other 8: they are not `pypi_fix_released`). The arms differ ONLY in the
harness tree (fresh baseline vs the sibling worker's loop-fix tree).

| task | side used | package digest | verifier digest | variant status |
|---|---|---|---|---|
| 000383 | original | `da5de502…` | `0dad7115…` | — |
| 002256 | variant `8f0e6de1d93b` | `8f0e6de1…` | `82168f27…` | validated |
| 002391 | original | `d0a399b8…` | `a29239bb…` | — |
| 001832 | original | `7a03868f…` | `ef5eb120…` | — |
| 001896 | original | `a616c127…` | `b80b55f3…` | — |
| 002864 | variant `25b94c8b4e4e` | `25b94c8b…` | `b7066b5b…` | validated |
| 001161 | original | `52b8743e…` | `1963b1cd…` | — |
| 000495 | original | `0cddeb65…` | `357bbde5…` | — |
| 001181 | original | `a680b2bd…` | `262ebb6e…` | — |
| 000587 | original | `41e76938…` | `ad8e0c65…` | — |

(Full 64-hex digests in `tasks.json`. Parent digests equal package digests
for originals; the two variant parents are `062cfbc3…` (002256) and
`46cd97ec…` (002864). Variant verifier digests equal their parents': the
leak-close touches setup/blocklist only, not tests.)

## 2. Part B: leak study (5 tasks x {original, leakclosed} = 10 specs)

Both arms run on the BASELINE tree; they differ only in the task bytes
(PyPI open vs leak-closed `leak-close-pypi@1` variant).

**Selection rule.** Eligible pool = merged
`research/experiments/har108-python-census/task_health.parquet` rows with
`label == sound` AND `leak_channel == pypi_fix_released` (187 rows: the
HAR-108 census plus the HAR-113 unknown-wave relabels; the pre-HAR-113
worktree copy has only 97), minus the 10 Part A tasks, minus the 2 forced
includes = **183** tasks. Rank by `sha256("har116-leak-v1:<task_id>")`
ascending (same convention as HAR-110's `make_split.py`), take the first 3.
Forced includes per the card: 000226, 000927.

Note on 000927: it reads `sound` in `task_health` (HAR-108 nop evidence);
one HAR-111 hand rater labels it broken, the other sound. The card orders
it in, and the census nop plus its validated leak variant agree it runs.

| task | how selected | original digest | variant digest | verifier (both) | variant status |
|---|---|---|---|---|---|
| 000226 | forced | `54bfbfa4…` | `9666803b…` | `f338bf64…` | validated |
| 000927 | forced | `5dd33cce…` | `0b8d70b5…` | `6356e9f0…` | validated |
| 000146 | hash #1 | `c1b579f7…` | `3415583f…` | `acc3bb95…` | candidate |
| 002308 | hash #2 | `8911bbfa…` | `2d2501dc…` | `945effcb…` | validated |
| 002402 | hash #3 | `5ca085f1…` | `c91c3948…` | `aa3e88ac…` | candidate |

`make_specs.py` re-derives this selection from the census on every run and
refuses if it differs from the manifest; adopting a new selection is a
manifest edit, not silent drift. (`tests/test_har116_selection.py` pins the
rule's behaviour: eligibility, exclusions, determinism, input-order
independence.)

## 3. Generating the specs

```bash
uv run --no-sync python research/experiments/har116-loopfix-leak/make_specs.py \
  --baseline-tree <path> --baseline-digest sha256:<64hex> \
  --loopfix-tree <path> --loopfix-digest sha256:<64hex> \
  [--out-dir research/experiments/har116-loopfix-leak/specs]
```

- Trees are validated with `load_harness_tree` (pin + harness validity)
  and stored repo-relative. The sibling harness-variant worker supplies the
  two tree paths/digests.
- Route, environment and per-trial limits come from the retained HAR-110
  base spec (`base-specs/student-terminus2-selfhosted-python.json`); spec
  names are `har116-a-<task>-<baseline|loopfix>` and
  `har116-b-<task>-<original|leakclosed>`, `question_ref`
  `har116-loopfix-leak`, attempts/concurrency 1.
- Staging: originals are copied from the pinned read-only snapshot
  (`derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks`,
  falling back to the primary checkout's copy), variants are rebuilt with
  `materialize` (proves the record invariant) and copied. Copies are made
  owner-writable per the task-variants convention. Every staged side is
  asserted against the manifest (package + verifier) and the run refuses on
  drift. Staging is idempotent: verified bytes are swapped into place, never
  mutated, so a re-run resumes cleanly.
- Validate without submitting (submit has no dry-run; this reuses its exact
  admission checks — `read_spec`, digest-vs-disk, `load_harness_tree`):
  `make_specs.py --check <specs-dir>` (needs the same tree args).
- Do NOT submit to the queue from this prep: submission parks specs in
  `queue/waiting/` for the parent's launch.

## 4. Proof (placeholder trees, $0)

Generated with the CURRENT HAR-110 tree
(`research/experiments/har104-mimo-exploration/harness`,
`sha256:433d5d29…`) as a placeholder for BOTH trees into a temp dir, then
`--check`:

```
warning: baseline and loopfix trees are identical (placeholder proof only)
part B pool: 183 eligible; selected: format-code-task-000146, format-code-task-002308, format-code-task-002402
wrote 30 specs + cohort.json to /tmp/har116-proof
route terminus-2 + selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on daytona; per-trial est $1.85; total est $55.50
check ok: 30 specs + cohort.json in /tmp/har116-proof
```

## 5. Cost estimate (one 30-trial round vs the $5 card total)

Inputs: HAR-110 RESULTS round splits (7-trial round $1.21 Modal, 14-trial
round $1.70 Modal) at the server rate $2.8149/h (`BUDGET.md`; v2-r2's 14
trials took 37 min wall: 2.8149 x 0.62 h = $1.74 ~= $1.7); Daytona code
rate $0.23094/h for 2 vCPU + 8 GiB + 10 GiB (BUDGET.md; HAR-110 never
measured Daytona — its SDK was absent, so the sandbox line below is
rate-card arithmetic on the ~15 min nominal trial wall, with verifier
backstop 2100 s as the tail risk).

| line | math | nominal |
|---|---|---|
| Modal server (1 container, `max_containers=1`) | ~2 waves x ~40 min wall x $2.8149/h | ~$3.7 |
| Modal warm (cold start + 300 s idle tail) | 1 warm if waves run back-to-back | ~$0.4 |
| Daytona sandboxes | 30 trials x ~0.25 h x $0.23094/h | ~$1.7 |
| **Total nominal** | | **~$5.8** |

That is OVER the $5 card total in the nominal case, and the error bars
point the wrong way: 14-way sharing already stretched HAR-110 trial walls
~3x (12.5 min nominal work in a 37 min round), and per-trial sandbox walls
under contention are unmeasured. A clean fast round (uncontended ~12 min
trials, one warm) fits at ~$3.8; a contended round can reach ~$8.

Recommendations for the launch:

1. **Do not run all 30 at once.** Run Part A wave 1 first (10 trials,
   `tick --parallel 10`), reconcile BOTH bills (`evallab modal
   billing-reconcile` + the Daytona org billing) against §5 lines, then
   size the rest. Per-trial Modal cost FALLS with sharing ($0.173 at 7-way
   vs $0.121 at 14-way), so prefer fewer, fuller waves — but see (2).
2. **`tick --parallel 30` is not feasible.** The Modal app runs a single
   container (`tools/modal-mimo-serve/serve.py`: `max_containers=1`), and
   SGLang sizes decode CUDA graphs to batch 16
   (`--cuda-graph-max-bs-decode 16`). 30 concurrent long-context trials
   would queue past the capture batch into unmeasured slowdown; the proven
   maximum is 14 parallel (HAR-110 v2-r2). Cap waves at **14**.
3. **Queue arithmetic blocks a single-day submit.** Catalog spend sums
   `est_cost_usd`: 30 x $1.85 = $55.50 against the $20/day ceiling
   (`policy/standing-approvals.yaml`; per-job $3 is fine at $1.85). Submit
   in waves of <= 10 specs/day ($18.50) unless Peter raises the ceiling.
4. The loop-fix arm should cut runaway-loop walls (that is the experiment),
   and leak-closed variants may finish earlier — both push toward the $3.8
   end. Measure wave 1 before claiming it.
