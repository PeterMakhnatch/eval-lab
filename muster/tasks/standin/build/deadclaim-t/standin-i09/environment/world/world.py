import json
import threading
from pathlib import Path

import worldkit
from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")
MAX_STEPS = 64
CHUNK = 4096
MAX_CHUNKS = 1024


def matches(on: str, event: dict) -> bool:
    if on == "none":
        return False
    return on == "any" or on == f"{event['method']} {event['kind']}"


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
        self.armed: dict[tuple[int, int], dict] = {}
        self.fired: set[int] = set()
        self.real_rooms: set[int] = set()
        self._fire([], None, [-1])

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
            reading = (method, path.strip("/")) == ("GET", "msgs") and self.tokens.get(token or "") == self.real
            if reading and not self.closed and self._timed_armed():
                self._fire(self._late(worldkit.time.time()), None, [])
            before = self.seq
            status, response = super().handle(method, path, token, body, client)
            if status == 200 and self.seq == before + 1 and not self.closed:
                event = self._last_event()
                if event["seat"] == self.real:
                    self._after(event)
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

    def _blocked(self, trigger: dict) -> bool:
        return bool(set(trigger["unless_opened"]) & self.real_rooms)

    def _timed_armed(self) -> bool:
        return any(self.script[i]["triggers"][j]["timed"] for i, j in self.armed if i not in self.fired)

    def _late(self, now: float) -> list[int]:
        due = set()
        for (index, number), arm in self.armed.items():
            trigger = self.script[index]["triggers"][number]
            if index not in self.fired and trigger["timed"] and not self._blocked(trigger):
                if now >= arm["t"] + self.wait_sec:
                    due.add(index)
        return sorted(due)

    def _after(self, event: dict) -> None:
        if event["kind"] == "open":
            self.real_rooms.add(event["response"]["room"])
        due = set()
        for (index, number), arm in sorted(self.armed.items()):
            if index in self.fired:
                continue
            trigger = self.script[index]["triggers"][number]
            arm["seen"] += matches(trigger["on"], event)
            if self._blocked(trigger):
                continue
            counted = trigger["on"] != "none" and arm["seen"] >= trigger["count"]
            if counted or (trigger["timed"] and event["t"] >= arm["t"] + self.wait_sec):
                due.add(index)
        self._fire(sorted(due), event["t"], sorted(due))

    def _fire(self, due: list[int], t: float | None, anchors: list[int]) -> None:
        due = list(due)
        self.fired.update(due)
        for _ in range(MAX_STEPS + 1):
            ready = []
            for index, step in enumerate(self.script):
                for number, trigger in enumerate(step["triggers"]):
                    if index not in self.fired and (index, number) not in self.armed and trigger["after"] in anchors:
                        ready.append((index, number, trigger))
            if not ready:
                break
            anchors = []
            for index, number, trigger in ready:
                self.armed[(index, number)] = {"seen": 0, "t": t}
                immediate = trigger["count"] == 0 and trigger["on"] != "none" and not self._blocked(trigger)
                if immediate and index not in self.fired:
                    self.fired.add(index)
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
