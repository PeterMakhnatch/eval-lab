You are @SEAT@, the only agent in this task. There are no other agents.

Warehouses @WAREHOUSES@ each keep a ledger of @ROWS@ rows `{"item": <name>, "qty": <1 to 99>}`; an item can have several rows. The items are @ITEMS@. Every item is in at least @MIN@ ledgers.

@READS@

You must report the total quantity of each item over all @NW@ ledgers.

The world is an HTTP API at `$WORLD`. Every request needs your token:

    curl -s -H "Authorization: Bearer $SEAT_TOKEN" "$WORLD/ledger"

- `GET /rules`: the agents, the items, who reads which ledger.
- `GET /ledger`: your rows, each with its warehouse.
- `POST /answer` with `{"totals": {"<item>": <n>, ...}}`: your answer, a whole number for each of the @NI@ items.
- `GET /status`: whether you have answered.

The first accepted answer is final and cannot be changed; later answers are refused. A malformed answer is refused and does not count.

Score: the number of items with an exactly right total in the accepted answer, divided by @NI@; 0 without an accepted answer.
