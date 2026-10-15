---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# MiMo-V2.6 RL code tasks: what was broken, what Eval Lab fixed, how it is verified

Start here. This page is the index for the MiMo cleanup programme of
2026-10-09 to 2026-10-11. Each linked page owns its details; this page only
says what is true now and where the evidence lives.

**Current build target:** `research/experiments/mimo-clean-v6/manifest.csv`
(2,698 code tasks: 2,666 built, 32 skipped), using `separate-verifier@10`.
Build it explicitly with `evallab mimo-clean build --clean-version mimo-clean-v6`.
The v6 receipt distinguishes construction, repeated controls, and remaining
verification; a built package is not certified clean.

The refreshed v5 manifest remains available for historical comparisons.
Its #848 snapshot-hook update changed 2,664 package digests: their old grades
cannot certify the new bytes and are now `unverified:package-changed`.
Historical census rows retain their original digests; prior exclusions remain
admission holds. See `mimo-clean-v5-census/digest-refresh.csv` and the
[verification contract](verification.md) before using either set.

## Why the published data cannot be used as shipped

Two upstream layers, neither fixed:

- **Xiaomi** (`XiaomiMiMo/MiMo-V2.6-RL-oss` images, `mimoagent@467f0a1`): the
  upstream fix is still inside most images. The anti-hack preprocessing that
  Xiaomi describes is not in the public release; the harness's git strip is
  never called for code tasks.
- **FineEnvs** Harbor conversion (1.1 → 1.3.0): no image was ever rebuilt; the
  git strip only runs when a branch points past the base, so unreachable fix
  objects survive (Vals: 67% of code tasks). The grader runs as root in the
  agent's container. Details and a draft upstream report: [fineenvs-audit.md](fineenvs-audit.md).

## Every problem, and what closes it

| Id | Problem | Closed by (in the v5 chain) | Evidence |
|---|---|---|---|
| V1 | Fix commit on a branch past the base | `strip-future-history@1` | [vals-routes.md](vals-routes.md) |
| V2 | Fix commit kept as unreachable git objects | `strip-future-history@1` (rebuilds `.git` from the base closure) | same; ladder `git_history` 1.3.0+ |
| V3 | File mtimes point at the files the fix touches | `mtime-normalize@2` (works on Modal's lazy layers) | [vals-routes.md](vals-routes.md) |
| V4 | Agent parses pack files itself when git is blocked | closed by V2 (objects are gone) | same |
| V5 | Build/module caches hold the fixed tree | `purge-build-caches@4`; `@5` for 20 node build-output tasks | [vals-routes.md](vals-routes.md), [node-build-leak.md](node-build-leak.md) |
| V6 | Download the fixed release / upstream over the network | `agent-network-none@1` (agent phase only; graders keep network) | [vals-routes.md](vals-routes.md) |
| V7 | Prompt wording | opt-in `instruction-explicit-rules@1`, **not** in the chain (changes the task) | [vals-routes.md](vals-routes.md), [mimoagent-harness.md](mimoagent-harness.md) |
| E1 | Installed/build/venv/stray copies of the fixed project | `purge-installed-copies@2` (all Python tasks; copies reset to base bytes) | `research/experiments/purge-installed-copies-v2/` |
| E2 | Grader tampering (tracked conftest, sitecustomize, PATH shims, reward writers, in-source exit, pytest patching, module-level skips, Go `TestMain`/`os.Exit`, JS `process.exit`) | `separate-verifier@8` | [separate-verifier.md](separate-verifier.md) |
| E3 | Grader in the agent's container, agent is root | `separate-verifier@8`: fresh verifier, own checkout and base, takes only the agent's file changes | same |
| E4 | No reference fixes ship | 564 recovered from leaked history and replayed (oracle 1, do-nothing 0) | `research/experiments/mimo-reference-fixes/` |
| E5 | Broken environments | Python ledger repairs; 32 discarded; 11 `env-broken` in v5 | `research/experiments/python-task-ledger/` |
| E6 | Graders that download at test time | verifier keeps network; prefetch where needed | [vals-routes.md](vals-routes.md) |

The grader went through eight versions in three days. Each step closed a hole
or a false 0 found by measurement. `@8` grades by **fail-to-pass against the
verifier's own pristine run**: tests that fail without the change must now
pass (not be skipped), and passing tests must not break. The version history
and the reason for each step are in [separate-verifier.md](separate-verifier.md).

## Tasks excluded even after cleaning

- `fail:open-leak:env-dependency` (5): the answer is readable in a legitimate
  dependency the environment needs (001478, 001671, 001945, 001971, 002414).
- `fail:open-leak:node-build-pnpm` (1): 000928.
- `fail:vacuous-grader:compile-only` (1): 001694; `fail:unfair-task:*` (5):
  001833, 000865, 002661, 000803, 000838 (2026-10-14 triage below).
- `env-broken` (11) and the 32 ledger discards.

## How "clean" is verified

Four checks per task, all on the exact package digest in the manifest
([verification.md](verification.md)):

1. do-nothing agent scores 0 and the tests actually ran;
2. the reference fix scores 1 (where one exists);
3. the cheat ladder (`evallab cheat run`, 14 deterministic attacks, no model) scores 0;
4. a fix-content probe finds no copy of the fix anywhere in the container
   (blob hashes plus distinctive lines, with a positive control on the
   published image).

`verify` is `pass` only when every applicable check ran on that exact digest.
Most rows are `unverified:<reason>` because the paid census sampled the fleet
rather than running all four checks on all 2,666 packages. `unverified` here
means "not run on this digest", not "found broken".

Measured results:

| Check | Result |
|---|---|
| Cheat ladder on clean packages | 0 cracked across all census rounds (v3 283 scored, v4 100-task sample, v5 261 clean + 7 unscored). On the published packages the same ladder cracks them (e.g. 002402, 002552). |
| Fix-content probe | 12 real leaks found on earlier clean chains, all closed in v5 (11 Python copies by `purge-installed-copies@2`, 000919 by `purge-build-caches@5`); v5 re-probe of those 12: 0 hits. 6 not fixable by a transform → excluded above. |
| Reference fixes under the v5 grader | 87 graded 1 and 4 `1-noexec` in the v5 census; remaining `fail:0` cells are triaged in the receipt (1 grader shape open: 001431). |
| Do-nothing | 0 everywhere it ran; `0-noexec` rows lack proof the tests ran and stay unverified. |

Receipts, newest first: `research/experiments/mimo-clean-v5-census/`,
`mimo-clean-v4-census/`, `mimo-clean-census/` (v3), `fix-probe-v2/`,
`cheat-tamper-ladder/`.

## Open items

- Full-fleet coverage: 1,729 built tasks have no ladder run on v5; 294 lack a
  do-nothing run; 172 reference tasks await a v5 probe. The receipt lists the
  exact commands and the measured cost per check.
- 001431 (pytest collection-error shape) is graded 0 with a working fix.
- Next rebuild should move the whole fleet to `purge-build-caches@5`
  (kept selective in v5 to avoid re-grading 405 banked rows).
- 1,518 non-Python tasks: 136 have reference fixes; the rest of the Go and
  JS/TS reference sweep was budget-stopped.
- Not covered by this programme: MiMo general (925), webdev (2,093), cyber,
  music and terminal domains beyond the earlier per-domain ledgers.

## Running MiMo agents on the clean set

[mimoagent-harness.md](mimoagent-harness.md): Xiaomi's harness on the clean
packages, with the opt-in antihack guard and explicit-rules addendum recorded
per trial. Default runs keep both off; the task-level fixes above are what
make the tasks clean.

## Vacuous / unfair triage (2026-10-14)

The fleet grader scan (`vacuous.json`: 35 rows, 28 unique tasks) and the
unfair-task list in the own-runs scan were triaged task-by-task against
the raw `test.patch` commands and `separate-verifier@8` (which grades
silent/custom families, including `tsd`/`tsc`, by exit-code fallback, and
compares fail-to-pass across the whole `base && new` command). Result:
6 tasks excluded in the v5 manifest — `001694`
(`fail:vacuous-grader:compile-only`: the test binary is compiled and
never run), `001833` (`fail:unfair-task:non-idempotent-test`), and
`000865`/`002661`/`000803`/`000838`
(`fail:unfair-task:prompt-misaligned`) — and 27 retained with a note:
type-check tasks whose `tsd`/`tsc` assertions are the real test,
compile-check steps that sit alongside real unit tests, syntax checks
alongside real runners, and `base && new` tasks whose vacuous base phase
only costs regression coverage while the new phase runs real
fail-to-pass tests. Two terminal candidates (`candidate-1789`,
`candidate-1702`) are env-broken with never-combined single-transform
fixes and are not in this manifest. Full per-task table, evidence, and
`qa.finding/v1` records: `research/experiments/mimo-clean-v5-census/README.md`
(section “Vacuous / unfair triage”) and
`research/experiments/mimo-clean-v5-census/findings/`.
