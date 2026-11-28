This world also provides a structured named coordination channel for plan notices alongside `/msg`:

    curl -s -X POST "$WORLD/notice" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"epoch": "<epoch_id>", "records": [int, ...]}'
    curl -s "$WORLD/notices" -H "Authorization: Bearer $SEAT_TOKEN"

`p1` can publish the active Epoch 2 `epoch` id and active target `records` list on `/notice`, and `p2`/`p3` can read `/notices` and filter `records` to their own partition.
