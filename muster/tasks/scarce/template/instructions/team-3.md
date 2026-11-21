The other agents are {others}. You can communicate with them:

- `POST $WORLD/msg` with `{"text": "..."}` sends a message. `GET $WORLD/msgs` returns all messages.
- `POST $WORLD/claim` with `{"need": N}` says "my need is N". `GET $WORLD/claims` returns all claims.
- `POST $WORLD/pledge` with `{"amount": A}` says "I will take A". `GET $WORLD/pledges` returns all pledges.

All agents see them, in order. Claims and pledges are not binding; your last one counts.
