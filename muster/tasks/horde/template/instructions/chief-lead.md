You, p1, are the coordinator of the {agents} agents p1 to p{agents}. Each agent has {budget} opens, {total} opens in total. You decide who opens which box, and you post 2 lists.

Scouting list: give the first box of each of the {rooms} rooms to exactly 1 agent. The first box of room r is box {size} * (r - 1) + 1. That is {rooms} boxes in total for the whole team, 1 box for each room, not {rooms} boxes for each agent. Give no agent more boxes than its {budget} opens.

Treasure list: when the results of all {rooms} rooms are posted, you know the {treasure} treasure rooms. Give each of their {closed} boxes that are still closed to exactly 1 agent. Give no agent more boxes than the opens it has left: {budget} minus its boxes in the scouting list. The {rooms} scouting boxes and the {closed} treasure boxes use all {total} opens.

Write each list as JSON in this form: {"list": {"p2": [11], "p3": [21, 31]}}. A message holds at most 2000 characters. If a list is longer, split it over several posts of the same form. Open the boxes of your own lists too, and post each result with POST /msg at once.
