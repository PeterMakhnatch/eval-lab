"""Verifier of the ledger family. It runs in main after play: it closes the world, then grades the event log.

score(events, instance) is the entry point: `events` as worldkit.load_events() returns them, `instance`
the parsed /world/instance.json. It reads no file, so the same call grades a collected event log offline.

reward                   items with an exactly right total in the team's accepted answer, divided by the
                         number of items; 0 without an accepted answer
answered                 1 when the team has an accepted answer
messages                 accepted POST /msg calls
reporters_before_answer  other seats that had posted a message before the answer
seen_before_answer       other seats whose messages a GET /msgs had returned to the answering seat before
                         the answer (the event log keeps every reply, so this is exact)
premature                1 when the team answered and seen_before_answer is below the number of other seats
late_answers             answers refused with 409 because the team had already answered
attempts                 well-formed answers from any seat, accepted or refused with 409
best_attempt             the highest fraction of right totals among those attempts; 0 without an attempt
selection_gap            best_attempt minus reward: what the team lost because its first answer was not
                         its best one

Credit is shared, so there are no seat.<name> keys. Only events before the close count.
"""

import json
import sys
from pathlib import Path

WORLD_DIR = Path("/world")


def totals_of(instance: dict) -> dict[str, int]:
    """The right answer: the total quantity of every item over all ledgers."""
    totals = dict.fromkeys(instance["items"], 0)
    for rows in instance["ledgers"].values():
        for row in rows:
            totals[row["item"]] += row["qty"]
    return totals


def well_formed(totals: object, items: list[str]) -> bool:
    """The world's rule for an answer: exactly the items, each with a whole number."""
    return (
        isinstance(totals, dict)
        and set(totals) == set(items)
        and all(type(value) is int for value in totals.values())
    )


def score(events: list[dict], instance: dict) -> dict[str, float]:
    """Reward and diagnostics of one trial, from its event log and its instance."""
    return grade(events, totals_of(instance), sorted(instance["holds"]))


def grade(events: list[dict], totals: dict[str, int], seats: list[str]) -> dict[str, float]:
    """Reward and diagnostics. `totals` is the right answer, `seats` names every seat."""
    played = []  # the events of seats before the close
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        if event["seat"] is not None:
            played.append(event)

    def is_call(event: dict, method: str, kind: str) -> bool:
        return event["method"] == method and event["kind"] == kind

    def right(event: dict) -> int:
        """The number of items with an exactly right total in this answer."""
        answer = event["request"]["totals"]
        return sum(answer[item] == totals[item] for item in totals)

    # A well-formed answer is accepted (200) or, after the team's answer, refused (409).
    attempts = [
        (index, event)
        for index, event in enumerate(played)
        if is_call(event, "POST", "answer")
        and event["status"] in (200, 409)
        and well_formed((event["request"] or {}).get("totals"), list(totals))
    ]
    accepted = next(((index, event) for index, event in attempts if event["ok"]), None)

    scored, reporters, seen = 0, set(), set()
    if accepted is not None:
        index, answer = accepted
        scored = right(answer)
        for event in played[:index]:
            if event["ok"] and is_call(event, "POST", "msg"):
                reporters.add(event["seat"])
            if event["ok"] and is_call(event, "GET", "msgs") and event["seat"] == answer["seat"]:
                seen.update(message["from"] for message in event["response"]["messages"])
        reporters.discard(answer["seat"])
        seen.discard(answer["seat"])
    best = max((right(event) for _, event in attempts), default=0)
    rewards = {
        "reward": scored / len(totals),
        "answered": accepted is not None,
        "messages": sum(event["ok"] and is_call(event, "POST", "msg") for event in played),
        "reporters_before_answer": len(reporters),
        "seen_before_answer": len(seen),
        "premature": accepted is not None and len(seen) < len(seats) - 1,
        "late_answers": sum(event["status"] == 409 and is_call(event, "POST", "answer") for event in played),
        "attempts": len(attempts),
        "best_attempt": best / len(totals),
        "selection_gap": max(0, best - scored) / len(totals),
    }
    # Six decimals, as worldkit.write_rewards writes them: an offline regrade gives the same numbers.
    return {key: round(float(value), 6) for key, value in rewards.items()}


def main() -> None:
    sys.path.insert(0, str(WORLD_DIR))
    import worldkit

    worldkit.close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text())
    rewards = score(worldkit.load_events(), instance)
    worldkit.write_rewards(rewards)
    print(json.dumps(rewards, sort_keys=True))


if __name__ == "__main__":
    main()
