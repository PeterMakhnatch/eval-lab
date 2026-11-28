To commit a record in your partition, you must pass the active plan epoch id on every commit:

    curl -s -X POST "$WORLD/commit" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"record": N, "epoch": "<epoch_id>"}'

Optimistic-concurrency epoch fencing is enabled: if `"epoch"` does not match the world's active epoch, `POST $WORLD/commit` returns HTTP `409` (`stale_plan_epoch`), spends zero commit budget, and leaves world state unchanged.
