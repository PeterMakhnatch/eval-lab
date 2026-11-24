import json
from pathlib import Path

from worldkit import Reject, ThreadingHTTPServer, World, serve

INSTANCE = Path(__file__).with_name("instance.json")
LISTEN_BACKLOG = 1024


class Horde(World):
    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        assert sorted(instance["seats"]) == self.seats
        assert all(seat[:1] == "p" and seat[1:].isdigit() for seat in self.seats)
        assert instance["rooms"] > 0 and instance["room_size"] > 0 and instance["budget"] > 0
        assert set(instance["treasure_rooms"]) <= set(range(1, instance["rooms"] + 1))
        self.channel = instance["channel"]
        self.budgets = {"open": instance["budget"]}
        self.room_size = instance["room_size"]
        self.boxes = instance["rooms"] * instance["room_size"]
        self.treasure_rooms = set(instance["treasure_rooms"])
        self.opened: dict[str, list[dict]] = {seat: [] for seat in self.seats}
        self.cursor: dict[str, int] = dict.fromkeys(self.seats, 0)

    def get_rules(self, seat: str) -> dict:
        rules = super().get_rules(seat)
        ordered = sorted(self.seats, key=lambda name: int(name[1:]))
        assert sorted(rules["seats"]) == sorted(ordered)
        return {**rules, "seats": ordered}

    def get_new(self, seat: str) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        start = self.cursor[seat]
        assert 0 <= start <= len(self.messages)
        self.cursor[seat] = len(self.messages)
        return {"messages": self.messages[start:]}

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
    ThreadingHTTPServer.request_queue_size = LISTEN_BACKLOG
    serve(Horde(json.loads(INSTANCE.read_text())))
