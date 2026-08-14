Status: review-wanted
Last: six-path harness + perf CI ratchet + engineering.md table; PIPELINE still unmerged
Next: open SPEED PR; merge only when gh pr checks is fully green
Blockers: PIPELINE PR is not on origin/main — criterion 4 (ingest+projection Polars/optimal on the merged path) is blocked

Harness location: `scripts/profile/`
One command: `uv run python scripts/profile/harness.py`

PIPELINE gate (2026-08-14): `gh pr list` has no PIPELINE PR (open or merged).
`origin/main` HEAD was `e758df6` (mentor review). Did not edit
`src/evallab/{atif,facts,digest}.py`.

Two full harness runs (scratch DB `evallab_speed_prof`, Harbor stubbed) agreed
on paths {ingest, projection, facts, digest, queue-tick-100, fleet-status}.
Ratchet pass on current numbers; artificial digest=500 ms vs 5 ms budget
failed with "exceeds budget".

DATABASE_URL during profile: `.../evallab_speed_prof` (refused `/evallab`).
