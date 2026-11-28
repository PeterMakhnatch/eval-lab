To commit a record in your partition:

    curl -s -X POST "$WORLD/commit" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"record": N, "epoch": "<epoch_id>"}'

Here `"epoch"` is optional (`{"record": N}` is also accepted); the server does not block commits carrying a stale epoch id.
