You are agent {seat}.

There are 72 work records, numbered 1 to 72, partitioned across 3 seats (`p1`: 1..24, `p2`: 25..48, `p3`: 49..72). Your assigned partition is `{your_records}`. You can commit only records in your partition, and you have a budget of {budget} successful commits.

Every call to the world needs your Bearer token (`$SEAT_TOKEN`). Inspect rules, your baseline plan, and your own commits with:

    curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"
    curl -s "$WORLD/plan" -H "Authorization: Bearer $SEAT_TOKEN"
    curl -s "$WORLD/mine" -H "Authorization: Bearer $SEAT_TOKEN"

{commit_doc}

At the start of the episode (Epoch 1), `GET $WORLD/plan` gives `initial_epoch`, 3 `initial_targets` in your partition, and `stage1_commits` ({stage1_commits}). Commit your first {stage1_commits} `initial_targets` in Stage 1 (`2` for `p1`, `1` each for `p2` and `p3`). As soon as global valid commits across all seats reach `trigger_threshold` ({trigger}), a mid-episode replan fires (`replan_triggered: true` on `GET $WORLD/plan`, with `valid_commits` masked at {trigger}) and the world shifts to Epoch 2: the remaining uncommitted Epoch 1 targets become stale, new Epoch 2 targets (`1` for `p1`, `2` each for `p2` and `p3`) become active, and committing non-active records during Epoch 2 incurs a heavy obsolete-action penalty.

{role}

{team}

Stop once your {budget} valid commits are complete or abstain once `replan_triggered` is `true` when no verified Epoch 2 targets are available.
