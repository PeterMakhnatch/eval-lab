"""The boxes world: numbered boxes in rooms. Every box of a treasure room holds a flag.

A seat opens one box per call, within its budget. The world answers the box, its room and whether
it holds a flag. No answer to a seat depends on what another seat opened: a seat sees only its own
opens. With a channel, the seats can also post and read messages (the kit's /msg and /msgs).
"""

import json
from pathlib import Path

from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")


class Boxes(World):
    """`instance` is the parsed instance.json: rooms, room_size, treasure_rooms, budget, channel."""

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        self.channel = instance["channel"]
        self.budgets = {"open": instance["budget"]}
        self.room_size = instance["room_size"]
        self.boxes = instance["rooms"] * instance["room_size"]
        self.treasure_rooms = set(instance["treasure_rooms"])
        self.opened: dict[str, list[dict]] = {seat: [] for seat in self.seats}

    def post_open(self, seat: str, body: dict) -> dict:
        box = body.get("box")
        if type(box) is not int or not 1 <= box <= self.boxes:
            raise Reject(400, f'send JSON {{"box": n}} with a whole number n from 1 to {self.boxes}')
        room = (box - 1) // self.room_size + 1
        opened = {"box": box, "room": room, "flag": room in self.treasure_rooms}
        self.opened[seat].append(opened)
        return opened

    def get_mine(self, seat: str) -> dict:
        return {"opens": list(self.opened[seat])}


if __name__ == "__main__":
    serve(Boxes(json.loads(INSTANCE.read_text())))
