import json
from pathlib import Path

from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")


class Field(World):
    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        assert instance["rooms"] > 0 and instance["room_size"] > 0 and instance["budget"] > 0
        assert set(instance["treasure_rooms"]) <= set(range(1, instance["rooms"] + 1))
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
    serve(Field(json.loads(INSTANCE.read_text())))
