# delphik-open-defect-tb2

Answer key over Terminal-Bench 2.0 task packages labelled from the Delphik
`open-defect` ledger: **47 items**, all `heldout` (**23 `fail` archival defect
claims** and **24 `pass` negatives**: **23 `weak_negative`** + **1
`refuted_claim`**). Early planning notes (`FOCUS.md`) cited "29 + 545 rejected
as negatives", where 29 was the raw artifact count before task-level
aggregation/exclusions (27 included artifacts across 23 positive tasks) and 545
was the cross-benchmark candidate pool in `open-defect` rather than this
pinned TB 2.0 checkout (which has 89 tasks total, yielding 23 sampled weak
negatives from the 60 unflagged tasks plus 1 refuted claim = 24 negatives).

## Source

- Defect ledger: Delphik `open-defect`, local clone `/tmp/open-defect` at
  git commit `49c659b5ba7714b0ae7db1dfaaf349868552c84d` (2026-10-14).
  Terminal-bench subset: `defects/terminal-bench/tasks/<task>/*.json`
  (29 artifacts over 24 dirs, including `_multi-task/` covering
  `qemu-alpine-ssh` + `qemu-startup`). These defects concern the
  `harbor-framework/terminal-bench-2` repo (Terminal-Bench 2.0), not TB 2.1.
- Packages: `harbor-framework/terminal-bench-2` cloned to
  `~/Developer/lab-extras/upstream/terminal-bench-2`, with a detached
  worktree at the pin: `~/Developer/lab-extras/upstream/terminal-bench-2-tb20`.
  Tasks live at the repo root (`<root>/<task>/task.toml`), so
  `package.root_hint` is that worktree path. TB2 `task.toml` files have no
  `[task].name`, so `task_id` is `terminal-bench/<dir name>`.
- Docker image convention: each task's `task.toml` `[environment]`
  `docker_image` is `alexgshaw/<task>:20251031`; the images are amd64.

## Licence

open-defect: code Apache-2.0 (`LICENSE`), data CC BY 4.0 (`DATA_LICENSE.md`).
Attribution: Delphik open-defect ledger, commit
`49c659b5ba7714b0ae7db1dfaaf349868552c84d`. Upstream GitHub issues/PRs remain
under their upstream terms. Task packages remain under the
terminal-bench-2 licence; this key contains only labels and digests.

## Pin

The repo has no tags. Pin = `f5b891cb4f7c20e306f9d05887628b43af740f43`
(2026-01-25, "Update Discord channel reference in README (#34)"), the last
commit before `53ff2b8` "Various task fixes for TB2.1" — i.e. the
Terminal-Bench 2.0 release state (`terminal-bench@2.0`, 89 tasks) before 2.1
changes. Every fix PR referenced by the included artifacts (PRs #14, #28, #36,
#38–#40,
#47, #48, #57, #68, #71–#73, #77, #82, #83, #86) was unmerged at the pin, so
the package bytes at `f5b891cb4f7c20e306f9d05887628b43af740f43` match the code
inspected by the ledger. However, several entries describe time-dependent
external or network drift rather than permanent deterministic failures (see
temporal caveats below). All 89 task dirs at the pin were verified to exist;
every included item's package exists at the pin.

## Label provenance

Positive items represent **archival defect claims** from Delphik's L2/L3
source-read audits (`decision_note` per artifact). In `items.jsonl`,
`label_strength: "confirmed"` records that the claim was confirmed in Delphik's
archival source-read ledger—not that it was re-verified by live container
execution in Eval Lab. Category per artifact was judged
from title + summary + decision_note (not `defect_type_main` alone); the
one-line rationale is in each `root_cause[].summary`, and
`root_cause[].source_class` carries the raw `defect_type_main` (falling
back to `audit_level`). `expected_category` = category of the first
root cause (root causes sorted by artifact id); `expected_categories` =
sorted union. 28 root causes over 23 tasks. Primary-category counts:
`broken` 16, `prompt_misaligned` 3, `reachable` 2, `weak` 1, `tamperable` 1.

| Artifact | Task(s) | Category |
|---|---|---|
| pr-82 | qemu-alpine-ssh, qemu-startup | broken |
| pr-36 | break-filter-js-from-html | broken |
| issue-75, pr-71 | build-cython-ext | broken |
| issue-59 | build-pmars | broken |
| pr-38 | build-pmars | prompt_misaligned |
| issue-76 | build-pov-ray | broken |
| pr-39 | configure-git-webserver | broken |
| pr-28 | crack-7z-hash | broken |
| issue-46 | filter-js-from-html | broken |
| issue-89 | filter-js-from-html | weak |
| issue-52 | git-multibranch | broken |
| issue-62 | gpt2-codegolf | weak |
| issue-67 | make-doom-for-mips | tamperable |
| issue-78 | make-doom-for-mips | broken |
| pr-72 | mcmc-sampling-stan | broken |
| issue-60 | model-extraction-relu-logits | reachable |
| pr-40 | mteb-retrieve | prompt_misaligned |
| pr-68 | path-tracing-reverse | broken |
| issue-61 | path-tracing | reachable |
| pr-48 | portfolio-optimization | broken |
| pr-57 | protein-assembly | broken |
| pr-83 | qemu-alpine-ssh | broken |
| issue-85 | raman-fitting | prompt_misaligned |
| pr-47 | rstan-to-pystan | broken |
| issue-45 | sam-cell-seg | prompt_misaligned |
| issue-84 | sanitize-git-repo | broken |

## Exclusions (artifacts whose defect is absent / not a grader defect at the pin)

- `reshard-c4-data` pr-14: source marks it `not_counted`
  (`repo_upkeep_or_new_task_not_health_defect`); a uv-install upgrade, no
  qa category applies. The task is also excluded from the negative pool
  because it does have an artifact.
- `make-doom-for-mips` issue-44: source marks it `not_counted` for
  terminal-bench@2.0 health (covered by PR #50); resource infeasibility is
  not an oracle/verifier defect in this taxonomy. The task remains a
  defect item via issue-67 and issue-78.

Temporal and archival judgment calls (`items.jsonl` retained unchanged):

- `build-pov-ray` (`issue-76`) is retained in `items.jsonl` with
  `expected_verdict: "fail"`, `expected_category: "broken"`, and
  `label_strength: "confirmed"` as a **historical/temporal defect label**, not
  a claim that live execution fails today. When issue #76 was filed, `solve.sh`
  failed because `povray.org` returned HTTP 403 (Cloudflare) to direct source
  downloads, and fix PR #77 remained unmerged at the pin. As Delphik's ledger
  notes (`resolution: fixed`), `povray.org` later restored direct downloads; a
  contemporary `S0` oracle run can therefore pass when `povray.org` serves the
  tarball, even though the pinned `solve.sh` still depends on that unpinned
  external URL.
- `make-doom-for-mips` (`issue-78`) is titled "v2.1" upstream, but its three
  cited root causes (404 dependency URL, missing MIPS/LLVM toolchain, and
  nondeterministic frame comparison) are present in the pinned TB 2.0 files.

## Negatives and refuted claim

Negatives: TB2 task dirs at the pin with no open-defect artifact (including
excluded ones) and no candidate thread naming them
(`candidates/**/harbor-framework__terminal-bench-2__*.json`, task named in
`task_names` or in title/body text), sampled
`random.Random(0).sample(sorted(pool), 23)` (pool 60), `weak_negative`.
Plus `torch-pipeline-parallelism` as `pass` / `refuted_claim`: candidate
issue #88 with `terminal_status: not_present` (the claimed
`transformers==4.15.0` verifier pin is not in the audited source).

## Split

`heldout` for all items. This is a held-out key: do not tune on it.

## Scoring and caveats

- **Archival labels vs live execution**: positive labels come from Delphik's
  source-read ledger and historical issue/PR reports at commit
  `f5b891cb4f7c20e306f9d05887628b43af740f43`. Drift defects (such as Debian 11
  apt mirror EOL, PyPI package changes, or the historical `povray.org` HTTP 403
  on `build-pov-ray`) depend on external network state at the time a trial runs,
  not solely on the pinned package bytes.
- **Weak negatives and refuted claim**: the 23 `weak_negative` items mean only
  that Delphik's ledger had no defect artifact or candidate thread naming that
  task at the pin (sampled from 60 eligible TB 2.0 tasks, not 545); the single
  `refuted_claim` item (`torch-pipeline-parallelism`) records a candidate issue
  (#88) that Delphik's source audit rejected as `not_present`.
- **Digest binding**: labels apply only to the exact `(subject.task_id,
  subject.task_digest)` at commit `f5b891cb4f7c20e306f9d05887628b43af740f43`;
  findings or task reports from any other checkout are counted in
  `digest_mismatches`.
- **Verified vs flagged recall and category vs root-cause match**:
  - `verified_recall` and `verified_false_alarm_rate` require a digest-bound
    `qa.finding/v1` record with a non-null `reproducer`, `fault == "eval"`,
    `verdict == "fail"`, and `refutation.status == "verified"`.
  - `flagged_recall` and `flagged_false_alarm_rate` combine standing unrefuted
    findings and unreproduced `report.signals` (`S1` rubric and `S0` leak
    signals).
  - `category_match_recall` checks coarse `category` agreement with
    `expected_category` / `expected_categories`, whereas
    `root_cause_match_recall` requires an explicit `root_cause_id` matching one
    of the Delphik artifact IDs in `root_cause[].id` (and is `null` when
    `root_cause_alignment_fraction == 0`). Coarse category agreement is not a
    root-cause match.
- **No paired discrimination**: this key contains standalone task packages
  rather than paired clean/cheat trial trajectories, so paired discrimination
  is neither claimed nor scored.

## Rebuild

```bash
~/Developer/eval-lab/.worktrees/qa-task-agent/.venv/bin/python research/answer-keys/delphik-open-defect-tb2/build.py
```

Defaults: `--open-defect /tmp/open-defect`,
`--tb2-root ~/Developer/lab-extras/upstream/terminal-bench-2-tb20`,
`--tb2-pin f5b891cb4f7c20e306f9d05887628b43af740f43`. The TB2 root must be
checked out at the pin (e.g. `git -C ~/Developer/lab-extras/upstream/terminal-bench-2 worktree add --detach ~/Developer/lab-extras/upstream/terminal-bench-2-tb20 f5b891cb4f7c20e306f9d05887628b43af740f43`).
Re-running reproduces `items.jsonl` byte-identically (verified by diff).
