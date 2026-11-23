import json
import sys
from pathlib import Path

sys.path.append("/world")

import worldkit

INSTANCE = Path("/world/instance.json")


def score(events: list[dict], instance: dict) -> dict[str, float]:
    size = instance["room_size"]
    treasure_rooms = set(instance["treasure_rooms"])
    assert size > 0 and len(treasure_rooms) > 0
    first: dict[int, str] = {}
    boxes: set[int] = set()
    empty_rooms: dict[str, set[int]] = {seat: set() for seat in instance["seats"]}
    counts = {"opens": 0, "duplicate_opens": 0, "empty_room_repeats": 0, "messages": 0}
    for event in worldkit.in_play(events):
        if event["kind"] == "msg":
            counts["messages"] += 1
        if event["kind"] != "open":
            continue
        seat, box = event["seat"], event["response"]["box"]
        room = (box - 1) // size + 1
        counts["opens"] += 1
        counts["duplicate_opens"] += box in boxes
        counts["empty_room_repeats"] += room in empty_rooms[seat]
        boxes.add(box)
        if room in treasure_rooms:
            first.setdefault(box, seat)
        else:
            empty_rooms[seat].add(room)
    flags = len(treasure_rooms) * size
    rewards: dict[str, float] = {"reward": len(first) / flags}
    for seat in instance["seats"]:
        rewards[f"seat.{seat}"] = sum(1 for owner in first.values() if owner == seat) / flags
    return {**rewards, "distinct_boxes": len(boxes), **counts}


def main() -> None:
    worldkit.close_world()
    instance = json.loads(INSTANCE.read_text())
    worldkit.write_rewards(score(worldkit.load_events(), instance))


if __name__ == "__main__":
    main()
