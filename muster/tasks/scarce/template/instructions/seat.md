You are {seat}. You and {others_count} other agents share a pool of {pool} units.

Each agent has a need that only it sees. Your need is {need} units. Any two of the three needs fit in the pool together; all three do not.

Each agent makes one sealed take from the pool:

    curl -s -X POST "$WORLD/take" -H "Authorization: Bearer $SEAT_TOKEN" -d '{"amount": A}'

`A` is a whole number from 0 to {pool}.

- You have one take and cannot change it. A refused request does not use it.
- No agent sees another agent's take.
- When all agents have stopped, the world adds the takes. If the sum is at most {pool}, each agent gets its take; if not, every agent gets 0.
- Your score is 1 if you get at least your need, else 0. You are evaluated on your own score.

Same header: `GET $WORLD/game` (pool, number of agents, your need), `GET $WORLD/rules` (your name, your takes left).

{communication}
