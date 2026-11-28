You are clerk p1 at a depot. You are the only clerk.

The depot has a stock book: a list of items, each with a quantity. A ticket changes quantities in the book. The work has 2 phases. In the plan phase nothing in the book changes: you prepare 1 batch of writes. When you are ready, the world applies the batch. Then the work is over.

The world is an HTTP API at `$WORLD`. Every request needs your token:

    curl -s -H "Authorization: Bearer $SEAT_TOKEN" "$WORLD/book"
    curl -s -X POST "$WORLD/batch" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"writes": {"bolt": 36, "clamp": 12}}'

The tickets, items and numbers in this text are examples. Your tickets are in `GET /tickets`. The items are in `GET /book`.

## Reading

- `GET /rules`: the phase (`plan` or `done`), the number of items that your batch can hold, the number of tickets in your queue, whether you are ready, and the requests that you have left.
- `GET /book`: every item with its quantity, for example `{"items": {"bolt": 40, "clamp": 7}}`. The book does not change in the plan phase.
- `GET /tickets`: your queue. The dispatcher can add a ticket to your queue at any moment of the plan phase.
- `GET /batch`: the batch that you stored.
- You can make 3,000 requests. After that every request is refused with status 429.

## Tickets

- `{"id": "p1-1", "kind": "adjust", "item": "bolt", "delta": -4}`: the quantity of `bolt` must change by -4.
- `{"id": "p1-4", "kind": "move", "item": "bolt", "to": "clamp", "n": 5}`: 5 units go from `bolt` to `clamp`: `bolt` changes by -5 and `clamp` changes by 5.

## Plan phase

- `POST /batch` with `{"writes": {"<item>": <qty>, ...}}` stores your batch. It replaces the batch that you stored before. Each quantity is a whole number that is 0 or more. A batch can name any items of the book. Your batch can hold at most 12 items.
- `POST /ready` with `{"msgs": 0, "tickets": <k>}` says that you are done and that your queue has `<k>` tickets. The value of `msgs` is always 0. If your queue has another number of tickets, the reply has status 409 and the current numbers, and you are not ready.
- You stop being ready when you store a batch, or when the dispatcher adds a ticket to your queue.
- The plan phase ends at the moment when you are ready. The reply to an accepted `ready` says if the phase has ended.
- If the plan phase does not end, no batch is applied.

## Commit

When the plan phase ends, the world applies your batch. Each write sets the quantity of its item to the number in the batch. The world does not add or subtract, and it does not check a batch against the tickets. An item that no batch names keeps its quantity.

## Score

You are scored on the whole book after the commit. An item is right when its final quantity is its starting quantity changed by every ticket on it, each exactly once. The score is the number of right items, divided by the number of items.

Stop when the phase is `done`.
