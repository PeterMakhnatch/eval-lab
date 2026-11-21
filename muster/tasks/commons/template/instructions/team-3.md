The other agents are {others}. You can communicate with them:

- `POST $WORLD/msg` with `{"text": "..."}` sends a message to all agents. `GET $WORLD/msgs` returns all messages.
- `POST $WORLD/pledge` with `{"amount": A}` is a pledge: it tells all agents what you will take. `GET $WORLD/pledges` returns all pledges, in order. You can pledge again: your last pledge counts. Pledges are public and not binding.
