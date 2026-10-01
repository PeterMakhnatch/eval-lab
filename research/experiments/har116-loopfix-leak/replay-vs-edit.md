# HAR-116 addendum: stop vs last real edit, rater check

## (a) Is the online onset the same rule?

Yes: same features, same thresholds, same run. The reported call
differs by construction: `loop_onset` is the run's first call, the
nudge is the first call the run qualifies (start+3 for a 4-command
run, start+9 for a 10-message run; a `both` tie nudges at the command
reach, which is the earliest a live agent can know). Code path:
`harbor_terminus._apply_loop_fix` -> `loopfix.live_loop_action` ->
`loopfix._onset` over `step_features` (signature from
`token_flow.normalized_command`, edit flag from `token_flow._is_edit`,
message runs at `probe03.LOOP_MIN_RUN` (10), command runs at
`token_flow.COMMAND_RUN_MIN` (4) with no edit inside — the feature set
`token_flow._loop_onset` scans. Over the 82 runs the replay nudge
starts the stored onset's run on 82 runs, disagrees on 0.

## (b) Stop vs last real edit on the 82 runs

Stops before the last real edit (live work cut): 4 of 82.
Stops with no earlier edit to compare: 14.
Scored-1 runs the break would cut: 2.

| run | trial | reward | stop | last edit |
|---|---|---|---|---|
| HAR-110 000495 | har110-000495-plain__mDTBFAW | 0.0 | 53 | 81 |
| HAR-110 002407 | gepa-terminus-2-format-code-task__rkLmWuE | 0.0 | 34 | 79 |
| HAR-81 candidate-2684-security-appsec | har81-p-d-candidate-2684-securit__uwoAzn7 | 1.0 | 51 | 78 |
| HAR-81 candidate-1271-media-games | har81-l-d-a2-candidate-1271-medi__2tAyTq3 | 1.0 | 46 | 91 |

## (c) Traces' 12 rated runs vs rater labels

Agreed loop-free (both raters `none`): 5 of 12. Break fires (stop) on 1 of them.

| trial | reward | rater_a | rater_b | nudge | stop |
|---|---|---|---|---|---|
| har110-001181-plain__AnjbfwU | 0.0 | none | repetition | 66 | None |
| har110-000495-plain__mDTBFAW | 0.0 | none | none | 48 | 53 |
| har110-001161-plain__RZ8qyUu | 0.0 | none | none | None | None |
| har110-000587-plain__5NNxU35 | 0.0 | none | none | 68 | None |
| gepa-terminus-2-format-code-task__CFCbfps | 1.0 | completion-claim | completion-claim | None | None |
| gepa-terminus-2-format-code-task__X2dMzMw | 0.0 | completion-claim | completion-claim | None | None |
| gepa-terminus-2-format-code-task__8FqvKUU | 0.0 | completion-claim | completion-claim | 45 | 50 |
| har110-000495-seed__dimG2yD | 0.0 | none | none | 87 | None |
| har110-dev-002864-cfe31418__g7pfCqC | 1.0 | none | none | None | None |
| har110-dev-002391-cfe31418__MvXkrso | 0.0 | repetition | repetition | 67 | None |
| har110-001161-gepa__kLkGSnR | 0.0 | repetition | repetition | 10 | None |
| har110-dev-000383-cfe31418__L7nGNNi | 0.0 | repetition | repetition | 33 | 38 |
