# ov-mutant-killer-loop-20261031 — closing the STING mutation-testing repair loop

Follow-up to `ov-sting-mutants-20261031` (W2-STING-MUTANTS): eight strictly-DEV MiMo graders accepted a semantic
mutant of the reference patch. Here each one gets a **mutation-guided behavioral repair**: one
`openrouter/~anthropic/claude-sonnet-latest` call writes a behavioral `unittest` file for the contract the surviving mutant breaks
(Round-1 diffs shown; Round-2 holdout mutants never shown), the file is hooked into `tests/test.sh` (same hook
shape as the task-quality repairs' `test_outputs.py`), and the package goes through
`evallab.full_hardening.harden(single_container=True, allow_degraded=True)` (`full-hardening@1`, #1693).

Then every upgraded package is executed on Modal (`ModalBackend` shortcut channel, one sandbox per cell):

| cell | must |
|---|---|
| `reference` (reference patch applied) | 1.0 |
| `nop` (no change) | 0.0 |
| `witness-<id>` (each recorded task-quality exploit candidate; repaired tasks only) | 0.0 |
| `r1-<op>-L<n>` (every Round-1 surviving mutant) | 0.0 |
| `r2-<op>-L<n>` / `r2base-<op>-L<n>` (2 fresh holdout mutants, upgraded vs base grader) | measured |

Run `W2-MUTANT-KILLER-R` (synthesis via `openrouter/~anthropic/claude-sonnet-latest`; the earlier fable run produced no results).
Results live in `results-r/` (`kill_mutants.py` writes there); trial dirs under `~/Developer/eval-lab-results/2026-11-01/w2-mutant-killer-r/`.
Disclosed revision rounds: 000079 and 000484 false-rejected the reference on first synthesis (round 1, 6/8 valid); each got one
revision with the failing output fed back (`results-r/feedback/`); 000484 needed a second revision because its L82 mutant survived.
Preregistration, report and gates: `research-context/bridge/2026-10-31-overnight/or-sonnet/raw/W2-MUTANT-KILLER-R/`.

## Why single-container hardening (L1–L3) and not L4

`full-hardening@1` for MiMo packages with the default separate verifier is `separate-verifier@10`, which replaces
`tests/test.sh` wholesale (patch-only grading in a pristine checkout). That would drop both the existing witness hook of
the repaired snapshots and the new behavioral hook, since both live in `test.sh` outside `tests/test.patch` (the
hidden test commands of six of the eight tasks are opaque `.build_env` tarballs, so the tests cannot be folded into
the patch's pytest invocation). The manifest records L4 as `open`; the T2 proof (`t2-unified-harden-20261031`) is the
executed evidence for the same single-container transform on Modal.

## Hook interpreters

Repaired snapshots reuse their own `test_outputs.py` command prefix verbatim. For the four originals the prefix comes
from the decoded hidden test command (`mimo_build_env.tar.gz.b64` → `.build_env/test_command.sh`, or
`mimo_test_command.sh`): `python3` (000682, 000931 after its `dev-switch`), pyenv `python` with `MPLBACKEND=Agg`
(000484, Python 2.7), `python` (003005).

## Files

- `kill_mutants.py` — cohort (DEV re-check, reference sha256), synthesis (`--synthesize`, ≤ 4 concurrent `omp -p`,
  served model read back from the session log), hook + harden, Round-2 generation, Modal cells (`--run`), summary.
- `results/synthesis.json` — per-task served model, cost, status, hook, hardening layers, package digest.
- `results/tests/<task>-test_mutant_behavior.py.txt` — the synthesized behavioral tests, verbatim as executed
  (`.txt` so the repo linter does not touch evidence; two of them are Python 2.7).
- `results/prompts/<task>.md` — the exact prompts; `results/manifests/<task>.json` — hardening manifests.
- `results/round2-mutants.json`, `results/r2-diffs/` — the holdout mutants (generated after synthesis, never shown).
- `results/plan.json`, `results/cells.csv`, `results/summary.json`, `results/table.md` — executed cells and gates.
- Trial directories: `~/Developer/eval-lab-results/2026-11-01/w2-mutant-killer/cells/<task>/<cell>/trial/`;
  upgraded packages: `.../w2-mutant-killer/packages/<task>/` (+ `.hardening-manifest.json`).

`evallab.mutant_auditor` is imported from this tree when PR #1696 is merged, else loaded from the
`ov-w2-sting-mutants-20261031` worktree copy (path recorded in `results/summary.json`).
