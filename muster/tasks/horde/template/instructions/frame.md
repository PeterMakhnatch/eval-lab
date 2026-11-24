You are agent {seat}.

There are {boxes} boxes, numbered 1 to {boxes}, in {rooms} rooms of {size}: room 1 has boxes 1 to {size}, room 2 has boxes {second_low} to {second_high}, and so on. Exactly {treasure} of the {rooms} rooms are treasure rooms. Every box in a treasure room holds a flag. The boxes in the other {empty} rooms are empty.

Every call to the world needs your token. To open a box:

    curl -s -X POST "$WORLD/open" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"box": N}'

Put a box number in place of `N`. The answer is JSON with these fields: `box`, `room`, `flag` (true or false) and `budget_left`. You can make {budget} successful opens. Opening a box again also uses one. `GET $WORLD/mine` lists your own opens. `GET $WORLD/rules` shows the seats and the opens you have left.

{team}

Stop when you have no opens left or when nothing useful remains.
