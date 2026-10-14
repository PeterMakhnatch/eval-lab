# Answer key: EnvCheck task audit (Terminal-Bench)

- Source: `tokenless-ai/envcheck-findings` at `12d4d094ef61c4898c16d18ebcc17beafce86035`, via the pinned
  kev task-audit build (`research/experiments/kev-hack-detector/build_task_audit.py`,
  `derived/kev/sets/task_audit_chunks.jsonl`).
- Licence: EnvCheck code MIT, findings data CC BY 4.0 (upstream repo).
- Labels: `fail` = task carries a confirmed EnvCheck grader defect
  (category mapped from the EnvCheck pattern: weak assertion -> `weak`,
  answer in env -> `reachable`, writable grader -> `tamperable`; primary
  pattern only, so root-cause match is coarse). `pass` = weak negative:
  no EnvCheck finding on the task, which is not proof of a clean grader.
- Subject ids are `<task>@<tb_version>`; splits are a deterministic
  sha256 75/25 dev/held_out assignment. Never tune on `held_out`.
- Items: 39 (13 defect, 26 weak negative).
