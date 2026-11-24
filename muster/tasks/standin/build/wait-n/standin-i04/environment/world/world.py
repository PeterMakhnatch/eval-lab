import json
import threading
from pathlib import Path

from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")
MAX_STEPS = 64
CHUNK = 4096
MAX_CHUNKS = 1024
ON = {"any": None, "GET msgs": ("GET", "msgs"), "POST open": ("POST", "open"), "POST msg": ("POST", "msg")}


def matches(on: str, event: dict) -> bool:
    want = ON[on]
    return want is None or (event["method"], event["kind"]) == want


class Standin(World):
    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        assert self.seats == [instance["real"]] and instance["real"] in instance["seats"]
        assert instance["budget"] > 0 and instance["wait_sec"] > 0 and len(instance["script"]) <= MAX_STEPS
        self.real = instance["real"]
        self.seats = list(instance["seats"])
        self.used = {seat: {} for seat in self.seats}
        self.lock = threading.RLock()
        self.channel = True
        self.budgets = {"open": instance["budget"]}
        self.room_size = instance["room_size"]
        self.boxes = instance["rooms"] * instance["room_size"]
        self.treasure_rooms = set(instance["treasure_rooms"])
        self.opened: dict[str, list[dict]] = {seat: [] for seat in self.seats}
        self.wait_sec = instance["wait_sec"]
        self.script = instance["script"]
        self.armed: dict[int, dict] = {}
        self.fired: dict[int, int] = {}
        self.real_events = 0
        self._advance(None)

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

    def handle(
        self, method: str, path: str, token: str | None, body: object, client: str
    ) -> tuple[int, dict]:
        with self.lock:
            before = self.seq
            status, response = super().handle(method, path, token, body, client)
            if status == 200 and self.seq == before + 1 and not self.closed:
                event = self._last_event()
                if event["seat"] == self.real:
                    self._advance(event)
            return status, response

    def _last_event(self) -> dict:
        with self.events_path.open("rb") as log:
            end = log.seek(0, 2)
            tail = b""
            for _ in range(MAX_CHUNKS):
                start = max(0, end - len(tail) - CHUNK)
                log.seek(start)
                tail = log.read(end - start)
                if start == 0 or tail.rstrip(b"\n").count(b"\n") > 0:
                    break
        event = json.loads(tail.rstrip(b"\n").rsplit(b"\n", 1)[-1])
        assert event["seq"] == self.seq
        return event

    def _due(self, event: dict) -> list[int]:
        due = []
        for index in sorted(self.armed):
            if index in self.fired:
                continue
            step, arm = self.script[index], self.armed[index]
            arm["seen"] += matches(step["on"], event)
            late = step["timed"] and event["t"] >= arm["t"] + self.wait_sec
            if arm["seen"] >= step["count"] or late:
                due.append(index)
        return due

    def _advance(self, event: dict | None) -> None:
        position = -1 if event is None else self.real_events
        if event is not None:
            self.real_events += 1
        due = [] if event is None else self._due(event)
        anchors = [-1] if event is None else list(due)
        for index in due:
            self.fired[index] = position
        for _ in range(MAX_STEPS + 1):
            ready = [i for i, step in enumerate(self.script) if i not in self.armed and step["after"] in anchors]
            if not ready:
                break
            anchors = []
            for index in ready:
                self.armed[index] = {"seen": 0, "t": None if event is None else event["t"]}
                if self.script[index]["count"] == 0:
                    self.fired[index] = position
                    due.append(index)
                    anchors.append(index)
        else:
            raise AssertionError("the script does not settle")
        for index in sorted(due):
            for action in self.script[index]["actions"]:
                self._act(action)

    def _act(self, action: dict) -> None:
        seat = action["seat"]
        assert seat in self.seats and seat != self.real
        if "open" in action:
            left = self.left(seat, "open")
            assert left is not None and left > 0, (seat, action)
            body = {"box": action["open"]}
            result = self.post_open(seat, body)
            self.used[seat]["open"] = self.used[seat].get("open", 0) + 1
            self._log(seat, "open", "POST", body, 200, {**result, "budget_left": self.left(seat, "open")})
            return
        body = {"text": action["post"]}
        self._log(seat, "msg", "POST", body, 200, self.post_msg(seat, body))


if __name__ == "__main__":
    serve(Standin(json.loads(INSTANCE.read_text())))
