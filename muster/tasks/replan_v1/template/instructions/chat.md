There are 3 seats (`p1`, `p2`, `p3`) sharing a free-text message channel:

    curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'
    curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"

Coordinate Stage 1 (`stage1_commits` initial targets per seat: `2` for `p1` and `1` each for `p2` and `p3` to reach the {trigger}-commit checkpoint), then share and adopt the Epoch 2 `epoch` id and target records before committing Stage 2 work.
