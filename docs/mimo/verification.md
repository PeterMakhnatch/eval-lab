# MiMo clean-set verification (fleet census)

Date: 2026-10-10. Owner: CensusFinish lane. Historical receipt:
`research/experiments/mimo-clean-census/` (`results.csv` + `README.md`).
The fleet runner and report producer are retired. Verifier log readers remain in `src/evallab/mimo_census.py`.
Restore the historical runner with `git checkout archive/pre-round3-2026-11-12 -- src/evallab/mimo_census.py`.

## What the census proves

For every clean-set manifest row, four checks on the FINAL clean package
(digest-bound, not the historical HAR-191 rows):

(a) **nop = 0 with tests executed** — the nop control grades 0 AND the
    trial carries recognized test-case evidence: countable JUnit/surefire
    reports, the verifier's explicit runner markers, or runner-appropriate
    result/summary lines in `verifier/test_output.log`. `named` is diagnostic
    only; `cases > 0` is sufficient. An exit code alone never proves case
    execution. `0-noexec` means no recognized execution proof, not a claim
    that a custom harness necessarily ran no tests; it remains unverified.
(b) **oracle = 1 with tests executed where a reference fix exists** —
    manifest `reference_fix` identifies the indexed HAR-191/sweep patch.
    Reward 1 without execution evidence grades `1-noexec`, never pass.
    Rows with `reference_fix = none` report `n/a`; that is not a solvability
    proof.
(c) **cheat ladder = clean, with its verdict basis named** — every attack of
    `evallab.cheat_ladder.ATTACKS` (v1.4.0: 14 attacks including
    `tamper_source_skip` and `tamper_source_skiptest`, executed through the
    model-free `cheat` agent; payloads reused, never forked) ends `executed`
    or legitimately `skipped` (per-attack inapplicability with a recorded
    reason — e.g. source-tamper attacks on tasks with no imported package
    `__init__`), and the benchmark's own verifier returns reward 0 for every
    trial. Reward 0 alone does not say how the verifier produced it. Each
    covered v6 row names its basis, derived from the cell's
    `verifier_decision`:

    - **tamper-refusal** — the verifier refused the agent diff on a tamper
      signature (`verifier_decision` is `tamper signature in agent diff`;
      `refusal_hit` names the matched file) and returned reward 0 without
      grading the task's tests. It proves the verifier detects that
      signature and refuses the tampered package. It does not prove the
      tests reject that attack or any other attack. All 80 v6 refusals
      matched one signature, `tamper \b_pytest\b` in a package
      `__init__.py` that a `tamper_source_*` attack planted.
    - **graded** — the verifier ran the task's own runner on the attacked
      tree and recorded its exit code (`verifier_decision` carries `rc=`),
      with reward 0. It proves the verifier graded the attacked package and
      the task's test run did not pass on it. It does not attribute that
      failure to one attack when several ran in the same trial, and it
      proves nothing about attacks that did not run: the 19 graded v6 rows
      ran 8–10 of the 14.
    - **unscored** — no verdict (no decision recorded, or no trial). It
      proves nothing about the ladder; the row is covered, not clean.

    A refusal-clean row therefore does not show the tests reject the other
    attacks; only a graded cell does. With all 4 `tamper_source_*`
    attacks removed, the remaining 10 attacks, graded together, returned
    reward 0 on all 7 rows tried; the source tampers stay ungraded on
    those rows. At the 2026-11-10 commit the v6 table holds 100 covered
    rows: 99 clean (80 tamper-refusal, 19 graded) and 1 unscored (`000928`,
    whose setup healthcheck failed before the agent ran). Per-row basis and
    counts are in
    [ladder-progress.csv](../../research/experiments/mimo-clean-v6/ladder-progress.csv);
    the graded subset cells are
    [ladder-graded-subset.json](../../research/experiments/mimo-clean-v6/ladder-graded-subset.json)
    and
    [ladder-graded-subset-p0.json](../../research/experiments/mimo-clean-v6/ladder-graded-subset-p0.json).
    The [separate verifier](separate-verifier.md) page records the detection
    and refusal contract.

    Missing/failed attacks grade `partial` (unverified). A cracked
    full-ladder trial triggers per-attack single trials for attribution;
    unattributed cracks report `full-ladder-unattributed`. Ladder cells are
    version-bound: a 1.3.0-clean carries 12/14 attacks only; the 2
    source-skip attacks need fresh 1.4.0 trials.

    Ladder v2 (`2.0.0`) exists alongside this census contract. It appends
    `leftover_test_swap`, `fifo_answer`, `fork_bomb`, and `disk_fill` to
    the frozen 1.4.0 ladder and is selected only explicitly
    (`evallab cheat run --ladder-version 2.0.0` or
    `EVALLAB_CHEAT_LADDER_VERSION=2.0.0`). Census and admissions stay on
    1.4.0 by default; `attempts.json` records the selected generation, so
    v2 cells are never reused as 1.4.0 evidence. The fork and disk probes
    are bounded (64 short-lived processes; 128 MiB) and clean up in-script.

    Ladder v3 (`3.0.0`) additionally includes the four HAR-207/HAR-208
    fake-success regressions: `tamper_pytest_runtest`,
    `tamper_pytest_runtest_obfuscated`, `pyc_shadow`, and
    `fake_unittest_output`. Select it explicitly with the same
    `--ladder-version`/environment contract. A v1/v2-clean row does not
    establish coverage of these attacks. Bind rejection, selected/armed
    evidence, and oracle/nop controls to the exact repaired package digest;
    synthetic wrapper regressions or controls on a rebuilt package cannot
    certify another input. The [HAR-208 receipt](separate-verifier.md#har-208-exact-input-acceptance-2026-10-17)
    records the exact-package lineage, native `oracle-x3`/`nop-x3` controls,
    and native `CheatAgent` ladder-v3 rejections for `002139` and `002866`
    without claiming fleet-wide certification.
(d) **fix-content census = 0 locations with a real positive control** —
    for all reference tasks, the historical fix-content probe stages signatures
    from the actual indexed patch and measures BOTH the published-image
    baseline setup and the ACTUAL shipped clean-package setup. No synthetic
    marker is injected, and no setup is recomposed. Published zero is
    `probe-blind`, even if the clean scan also finds zero; it cannot prove
    cleanliness. Setup, readiness, probe, and scan completion must all be
    evidenced before a clean location count can pass. Both outcomes and
    sandbox lifecycle are retained, including incomplete/blind controls.

## Backends and parity

- `docker` — local Docker, $0, honoring the package's declared network
  contract, never a census-only runtime egress lock. Historical v2 packages
  declare `network_mode = "public"`, so `upstream_fetch` had real agent
  egress. Canonical v3 adds `agent-network-none@1`: only the agent phase is
  blocked; setup and the separate verifier remain public. The ladder now
  measures that shipped restriction, not our backend override.
- `modal` — Harbor 0.24's native ModalEnvironment running the REAL clean
  package semantics: embedded-healthcheck setup, agent phase, workspace
  snapshot, then the bundled separate-verifier grading in a FRESH sandbox
  from the same pinned image. No HAR-191 shortcuts (oracle/nop sharing one
  sandbox); the fresh-verifier boundary is Harbor's own separate-verifier
  lifecycle. Requires the Modal SDK (`modal` in the `dev` group of `pyproject.toml`)
  plus `~/.modal.toml` auth.
  Historical v2 parity hit two now-fixed setup portability faults:
  mtime-normalize@1 (fixed by @2, PR #813) and purge-build-caches@3
  (fixed by @4, PR #823). In pip 25.3 any valid nonempty
  `PIP_NO_CACHE_DIR` value disables caching, including Modal's `"off"`;
  @3 misclassified that disabled state as a purge failure. The 3/3
  failed v2 parity cells remain historical `setup-fail` evidence.
  @4 has a full Modal final-setup proof (`rc=0`, `ready=yes`);
  final-v3 native reward/resource-policy parity is recorded separately below.
- `daytona` — bounded Daytona sandbox env (`DAYTONA_API_KEY`), an alternate
  control backend. Controls run under the default
  MiMo egress lock; the cheat agent is outside every lock set and
  Daytona+MiMo additionally refuses explicit unlocked, so cheat cells are
  refused at dispatch and recorded `backend-unsupported` (never failures).
  Exact mechanism (`src/evallab/execution_contracts.py`): MiMo+Daytona
  mandates `egress_lock=true` (explicit `false` refused, no silent unlocked
  fallback) and `EGRESS_LOCK_AGENTS["daytona"]` excludes `cheat`, because
  the ladder's network attacks (notably `upstream_fetch`: `git ls-remote`,
  `cheat_ladder.py`) need sandbox egress a locked sandbox cannot provide.
  Assessed 2026-10-10, no change: an unlocked Daytona MiMo run needs a
  policy change (out of lane), and admitting `cheat` to the lock set would
  weaken V6 (untestable under lock) while Docker ladders test it with
  egress. Ladders stay on Docker/Modal.
  Cheat coverage for those tasks comes from a backend that admits it as a
  SEPARATE results row: keys include task, backend, manifest version, exact
  digest, and Modal policy; historical generations are never relabeled.

Parity gate (required before any paid batch): on ≥5 tasks, local-Docker
Harbor results == census-runner docker-backend results for oracle/nop/ladder.
Modal batches additionally compare per-attack ladder detail (especially
`upstream_fetch`) against Docker with the same package network contract.
Controls and ladders retain their exact package digest/version; changing
the shipped agent network policy does not silently relabel earlier rows.

Modal census trials use the billing app `mimo-clean-census` and known-patch
fix probes use `mimo-clean-census-fix`, separating this slice from other
lanes' shared `__harbor__` usage. `RunRequest.modal_app_name` only forwards
Harbor's `app_name` environment argument; it does not grant paid approval
or modify the package's network policy.

`RunRequest.modal_resource_policy="auto"` is the unchanged Harbor default. The
census-only opt-in `"limit"` forwards `cpu_enforcement_policy=limit` and
`memory_enforcement_policy=limit`: the package's CPU and memory **caps stay
unchanged**, while Modal may bill a smaller idle request rather than the
full cap. The override is rejected outside Modal and outside the census
billing app. Each result records its policy; reusable cells must match it.
Before fleet use, compare AUTO and LIMIT on at least three tasks against
the Docker rewards, including the ladder, and check for OOMs/timeouts.

The final-v3 gate passed on 000085, 000158, and 002552: Docker, Modal AUTO,
and Modal LIMIT all returned oracle 1, nop 0, and a clean full ladder on
identical digests. All 18 controls have actual test-execution evidence; all
27 trials have no reported OOM, timeout, or trial exception. The exact
configs, caps, raw paths, and grades are in
`research/experiments/mimo-clean-census/modal-limit-parity.json`.
The direct SDK fix probes are separate AUTO/default-resource executions,
not native LIMIT trials; their lifecycle metadata records the actual SDK
path and the isolated fix-probe app.

