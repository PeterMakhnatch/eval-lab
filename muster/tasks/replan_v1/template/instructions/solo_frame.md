You are agent {seat} operating as the solo seat.

There are 72 work records, numbered 1 to 72. Your assigned partition is `{your_records}`. You can commit only records in your partition, and you have a budget of {budget} successful commits.

Every call to the world needs your Bearer token (`$SEAT_TOKEN`). Inspect rules, your baseline plan, and your own commits with:

    curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"
    curl -s "$WORLD/plan" -H "Authorization: Bearer $SEAT_TOKEN"
    curl -s "$WORLD/mine" -H "Authorization: Bearer $SEAT_TOKEN"

{commit_doc}

At the start of the episode (Epoch 1), `GET $WORLD/plan` gives `initial_epoch`, `9` `initial_targets` in your partition, and `stage1_commits` ({stage1_commits}). Commit your first {stage1_commits} `initial_targets` in Stage 1. As soon as valid commits reach `trigger_threshold` ({trigger}), a mid-episode replan fires (`replan_triggered: true` on `GET $WORLD/plan`, with `valid_commits` masked at {trigger}) and the world shifts to Epoch 2: the remaining 5 uncommitted Epoch 1 targets become stale, 5 new Epoch 2 targets become active, and committing non-active records during Epoch 2 incurs a heavy obsolete-action penalty.

Once `replan_triggered` becomes `true`, call `GET $WORLD/alerts` to inspect the active Epoch 2 `epoch` id and the updated `targets.{seat}` list, then commit only those active Epoch 2 targets.

Stop once your {budget} valid commits are complete.
