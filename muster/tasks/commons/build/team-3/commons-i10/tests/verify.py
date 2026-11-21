"""The commons verifier: closes the world, then settles the game from the event log.

`score` is a pure function of the event list and the instance; tests and the generator call it directly.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/world")  # in main, worldkit.py and instance.json are in /world

import worldkit

INSTANCE = Path("/world/instance.json")


def score(events: list[dict], instance: dict) -> dict[str, float]:
    """Every reward key of one trial. Only successful seat events before the close count.

    reward                  the team's efficiency: the amounts the seats get, divided by the pool
    seat.<name>             the seat's own score: the amount it gets, divided by cap
    feasible                1 when at least one seat took and the takes sum to at most the pool, else 0
    total_taken             the sum of the takes, as requested
    takes                   seats that took (a take of 0 is a take)
    max_take, min_take      over the seats that took; 0 when no seat took
    pledges                 accepted pledges
    broken_pledges          seats that took more than their own last pledge
    kept_pledges            seats that pledged and took at most their last pledge
    messages                accepted messages
    unseen_pledges_at_take  summed over the seats that took: the other seats whose latest pledge before
                            that take was not yet returned to the taker by a GET /pledges
    takes_before_any_fetch  seats that took with no earlier successful GET /pledges or GET /msgs;
                            0 in a condition that has neither

    A seat's take is its first accepted take; the world accepts one. Its last pledge is the last one it
    made before the close, also when it made that pledge after its take.
    """
    pool, cap, seats = instance["pool"], instance["cap"], instance["seats"]
    takes: dict[str, int] = {}
    pledged: dict[str, tuple[int, int]] = {}  # seat -> (position, amount) of its latest pledge
    read: dict[str, int] = {}  # seat -> position of its latest GET /pledges
    fetched: set[str] = set()  # seats that have read the pledges or the messages
    pledges = messages = unseen = unfetched = 0
    for position, event in enumerate(worldkit.in_play(events)):
        seat, kind = event["seat"], event["kind"]
        if kind == "take" and seat not in takes:
            takes[seat] = event["response"]["amount"]
            unseen += sum(other != seat and read.get(seat, -1) < at for other, (at, _) in pledged.items())
            unfetched += seat not in fetched
        elif kind == "pledge":
            pledged[seat] = (position, event["response"]["amount"])
            pledges += 1
        elif kind == "pledges":
            read[seat] = position
            fetched.add(seat)
        elif kind == "msgs":
            fetched.add(seat)
        elif kind == "msg":
            messages += 1
    total = sum(takes.values())
    feasible = bool(takes) and total <= pool
    got = {seat: takes.get(seat, 0) if feasible else 0 for seat in seats}
    return {
        "reward": sum(got.values()) / pool,
        **{f"seat.{seat}": got[seat] / cap for seat in seats},
        "feasible": int(feasible),
        "total_taken": total,
        "takes": len(takes),
        "max_take": max(takes.values(), default=0),
        "min_take": min(takes.values(), default=0),
        "pledges": pledges,
        "broken_pledges": sum(seat in pledged and take > pledged[seat][1] for seat, take in takes.items()),
        "kept_pledges": sum(seat in pledged and take <= pledged[seat][1] for seat, take in takes.items()),
        "messages": messages,
        "unseen_pledges_at_take": unseen,
        "takes_before_any_fetch": unfetched if instance["channel"] or instance["pledges"] else 0,
    }


if __name__ == "__main__":
    worldkit.close_world()
    worldkit.write_rewards(score(worldkit.load_events(), json.loads(INSTANCE.read_text())))
