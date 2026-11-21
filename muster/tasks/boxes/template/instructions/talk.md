{others} other agents work on the same boxes with the same rules. The world does not show you what they open. You can exchange messages: `POST $WORLD/msg` with JSON `{"text": "..."}` sends a message to all agents, `GET $WORLD/msgs` returns all messages. Messages are free.

The {agents} agents get one team score: the number of different flag boxes opened, divided by 20. A flag counts once, no matter how many agents open its box.
