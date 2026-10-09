---
name: reward-hacking-audit
description: Audit a task or benchmark for reward-hacking channels and weak verifiers with a V1-V8 static ledger, authored exploit scripts, verifier mutation testing, and model-free replay through the task's own verifier.
---

# Reward-hacking audit

Use when asked whether a task, task set, or benchmark can be gamed, or to reproduce a suspected cheat. The
method is BenchJack's shape (reconnaissance, flaw scan, exploit construction) with one correction: a
benchmark's own verifier is the only authority on whether an exploit scores.

## Phases

1. **Reconnaissance and flaw scan (free, offline).** `evallab hack scan <package>` writes a ledger over one
   Harbor task package: verifier isolation, answer material in the agent image, untrusted execution in the
   verifier, LLM judges, weak matching, fail-open handling, and granted authority. Findings cite `file:line`.
   They are claims about design, never proofs of exploitability, and an absent finding is not a certificate.
2. **Exploit authoring (reasoning; any coding agent).** Write one script per candidate channel into
   `<package>/controls/` or pass `--script`. Read the verifier, the environment build, and the instruction.
   The script runs as the trial's reference solution, so it may do anything a solution may do — including
   nothing at all about the task.
3. **Replay (free, local Docker, no model).** `evallab hack run <package> --execute` runs three controls
   through the task's own verifier: the reference oracle (must score 1), the no-agent control (must score 0),
   and each exploit script. The reward that comes back is the benchmark's grade, not a local judgment.
4. **Verifier mutation testing (free, local Docker, no model).** Exploit scripts find channels around the
   grader; most published grader defects are the other kind: the grader accepts a wrong answer (EnvCheck's
   "insufficient checking"). `evallab hack mutate <package> --execute` captures the reference solution's
   target files, grades a blank control per file, then grades one-edit mutants of the reference output.
   Read each survivor's diff against `instruction.md`: an equivalent mutant (unreachable branch, a
   `parents=False` on a directory that exists) is noise; a survivor that changes required behaviour is a
   candidate defect. Re-run one survivor with `--mutant <id>` before quoting it.
5. **Red/blue verifier campaign (spend-gated, model search).** `evallab vcheck run --manifest
   campaign.json --budget-usd X --approve "..."` plans for free; add `--execute` to spend.
   Per-task red agents (GLM 5.3, 30 steps) read the grader, coach blue-solver probes, and submit
   wrong answers the task's own verifier scores; harness controls (oracle 1, empty 0) outrank any
   claim. Confirm survivors with `evallab vcheck confirm <id> --verdict confirm|reject`, export
   with `evallab vcheck export <finding-id>`. Survivors are candidates: read each against the
   requirement map, discard equivalent mutants, never quote a clean run as a certificate.

```bash
evallab hack scan library/tasks/<task>
evallab hack run library/tasks/<task> --script controls/<channel>.sh      # plan only
evallab hack run library/tasks/<task> --script controls/<channel>.sh --execute
evallab hack mutate library/tasks/<task> --workers 2 --execute     # targets = declared artifacts
```

Receipts land under `runs/.reward-hack/<slug>-<ulid>/` (report, matrix, staged script digests) with the matrix
receipt under `runs/.executor/`. `# expect: 1` on the script's first lines records the author's prediction; a
mismatch is the signal worth explaining.

## Exploit legitimacy rules

A script counts as a reward hack only under all of these:

- It reaches the score **without performing the task's work** (no fix, no computation, no correct artifact).
- It uses only what the agent can observe and do **during** the run: no pre-patching the package, no editing
  the verifier's files on disk, no assuming host connectivity or gold data the task does not grant.
- It runs through the normal agent path (here: the reference-solution channel), not around the harness.
- Every claim in the write-up names the exact command and file that carries it.

Channels worth trying, in the order they usually pay: read the verifier's own material (hidden tests, gold
files, git history past the base, caches, timestamps); shadow the tooling the verifier later invokes (PATH,
`sitecustomize`, `.pth`, shell init files) in a shared container; forge the signal the grader parses (reward
files, stdout markers, exit codes); plant hooks the verifier loads (`conftest.py`, plugin autoload,
`sitecustomize`); fetch a released answer over the network; exploit fail-open aggregation (empty result sets,
suppressed errors, `|| true`); inject into an LLM judge's prompt; and, only last, look for plain grader bugs.

## Honesty requirements

- Report claims (scan) and evidence (scored replays) separately; never promote a finding to "hack" because it
  looks damning.
- A resisted script is a negative result for that channel on that revision. It is not a certificate, and it
  must not be reported as one.
- `task_broken` (no-agent control earns reward, or the reference solution fails) outranks hackability: fix the
  package before quoting any number from that run path.
- Baseline, no-agent, and exploit rewards are the benchmark's own scores; quote the job paths so anyone can
  re-read them.
- Tightening a task after a confirmed hack is a new task version, never an edit in place (`docs/task-variants.md`).

## Portability

The scan reads files; the legitimacy rules and channels are framework-independent. Only the replay adapter is
Harbor-specific: it stages the exploit as `solution/solve.sh` and lets the oracle channel run it. On another
harness, run the same script as the agent's action in that harness and read the harness's own score for it —
do not re-implement the taxonomy.
