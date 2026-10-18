# `tb2-to-tb21-v1`: TB2 -> TB2.1 Version-Diff Answer Key

`qa.answer_key/v1` key in the style of Terminal-Bench version-diff
benchmarks (TVB). Built deterministically by `build.py` from
`research/experiments/breaker-rediscovery-v1/tb2-diff.json`
(pins `harbor-framework/terminal-bench-2@69671fba` ->
`terminal-bench-2-1@7131e437`).

## Contents

- **26 modified tasks**: the PR #53 cohort whose TB2 package was
  changed in TB2.1 (verifier/instruction/environment fixes).
  - TB2 item: `expected_verdict = "fail"` — the old version carries
    the defect a defect-reproducer should find.
  - TB2.1 item: `expected_verdict = "pass"`.
- **26 unmodified controls**: format-only tasks (first 26 sorted by
  task_id). Both versions `expected_verdict = "pass"`.
- **52 canonical pairs** (`pairs.jsonl`): child = TB2 item, parent =
  TB2.1 item. The scorer loads pairs only from this file; without it
  paired discrimination is n/a.

## Scoring rule

A finding counts only if its reproducer scores **1.0 on the TB2
package and 0.0 on the TB2.1 package** (differential validation,
fresh containers, oracle=1/nop=0 controls on both versions). Any
validated reproducer on a control task is a **false discovery**;
the key targets 0% false discoveries on TB2.1.

## Exposure disclosure

12 of the 52 tasks (`breaker_bench_dev_exposure: true`) appeared in
`breaker-bench-v1` dev tuning pairs, where the agent scaffold was
tuned. Scores must disclose this; an unexposed-subset score should
be reported alongside the full-cohort score.

## Rebuild

```bash
python3 research/answer-keys/tb2-to-tb21-v1/build.py
python3 research/answer-keys/tb2-to-tb21-v1/build.py --check
```
