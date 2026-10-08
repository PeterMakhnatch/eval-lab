# Grid-parallel free controls (HAR-204)

`evallab grid run <grid.yaml>` fans out `{task dir or dataset ref, agent in
{oracle, nop, cheat}, attempts, concurrency, backend: docker}` cells through
the existing direct-execution path (`Executor.execute_direct`), one
`RunRequest` per cell, under a `ThreadPoolExecutor` capped by `--width` and
the standing two-trial Docker posture. Docker headroom comes from the existing
`docker_available_resources` / `docker_task_resources` clamp: per-cell
concurrency is clamped down to fit (declared task limits are never rewritten),
and unfit cells skip with reason instead of executing.

No new spend authority: any cell naming another agent or a non-docker backend
is refused for the whole grid before any cell runs, with the exact
`evallab submit <spec.json>` / `approve <spec-id> --actor peter` /
`tick --spec-id <spec-id>` commands to run it through the standing-policy
queue. Grid never submits, approves, or ticks.

`evallab grid compare <job-dirs...>` projects native job evidence only
(`results.load_job`): per-cell pass rate, cracked rate for cheat cells, N,
unscored, backend, Harbor rev. Scored means exception-free finite reward;
cracked means verifier reward >= 1. The `cheat` agent is owned by the sibling
`evallab.harbor_cheat:CheatAgent` builder; grid references it by name and
skips cheat cells with reason when the module is absent.

Live-Docker end-to-end is opt-in (`EVALLAB_GRID_DOCKER_SMOKE=1`): the laptop's
shared daemon currently fails admission on third-party unbounded containers,
so unit + fixture tests carry acceptance and the smoke skips with reason
`shared daemon not admitted` on refusal.
