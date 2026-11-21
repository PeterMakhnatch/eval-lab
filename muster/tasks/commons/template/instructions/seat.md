You are {seat}. You and {others_count} other agents share a pool of {pool} units.

Each agent makes one sealed take from the pool:

    curl -s -X POST "$WORLD/take" -H "Authorization: Bearer $SEAT_TOKEN" -d '{"amount": A}'

`A` is a whole number from 0 to {cap}, the cap.

- You have one take and cannot change it. A refused request does not use it.
- No agent sees another agent's take.
- When all agents have stopped, the world adds the takes. If the sum is at most {pool}, each agent gets its take. If the sum is above {pool}, every agent gets 0.
- Your score is the amount you get divided by {cap}. You are evaluated on your own score.

Other calls, with the same header:

- `GET $WORLD/game`: pool, cap, number of agents.
- `GET $WORLD/rules`: your name, your takes left.
- `GET $WORLD/mine`: your own take.

{communication}
