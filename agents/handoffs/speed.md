Status: review-wanted
Last: post-PIPELINE re-profile; ingest_and_project 46.77 ms; documented already-optimal; no Polars
Next: open SPEED PR from role/speed-2; merge when gh pr checks is fully green
Blockers: none

Harness location: `scripts/profile/`
PIPELINE is on origin/main as 3ba570c (#17). Fresh branch `role/speed-2`
(do not reuse squash-merged `role/speed`).

Merged-path measurement (scratch `evallab_speed_prof`, Harbor stubbed):
ingest 30.47 ms, projection 3.20 ms, ingest+projection 46.77 ms.
Already optimal on the 2-job fixture; Polars not added.
