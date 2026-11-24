import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOMS = 6
ROOM_SIZE = 10
TREASURE_ROOMS = 2
PATIENCE = 60
SETTLE = 8
STEPS = 400
MAX_MESSAGES = 2000
POLL_SEC = 1.0
REPORT = re.compile(r"Box (\d+) \(room (\d+)\) (holds a flag|is empty)\.")
OPENED = re.compile(r"I opened box(?:es)? ([0-9, and]+) in room (\d+); (?:they hold flags|it holds a flag)\.")
NOW = re.compile(r"I open box(?:es)? ([0-9, and]+) now\.")
CLAIM = re.compile(r"I claim box(?:es)? ([0-9, and]+)\.")
PROMISE = re.compile(r"I scout rooms (\d+) and (\d+) now and report both results soon\.")
LIST = re.compile(r"List from (p\d+), the coordinator\.")
PART = re.compile(r"(p\d+): ([0-9, and]+)\.")


class Refused(Exception):
    pass


def numbers(text: str) -> list[int]:
    return [int(number) for number in re.findall(r"\d+", text)]


def words(values: list[int]) -> str:
    assert values
    text = [str(value) for value in values]
    return text[0] if len(text) == 1 else f"{', '.join(text[:-1])} and {text[-1]}"


def room_of(box: int) -> int:
    return (box - 1) // ROOM_SIZE + 1


def boxes_of(room: int) -> list[int]:
    return list(range((room - 1) * ROOM_SIZE + 1, room * ROOM_SIZE + 1))


def report_text(box: int, room: int, flag: bool) -> str:
    return f"Box {box} (room {room}) {'holds a flag' if flag else 'is empty'}."


def infer(kinds: dict[int, bool]) -> dict[int, bool]:
    values = list(kinds.values())
    rest = [room for room in range(1, ROOMS + 1) if room not in kinds]
    if values.count(True) == TREASURE_ROOMS:
        return {**kinds, **dict.fromkeys(rest, False)}
    if values.count(False) == ROOMS - TREASURE_ROOMS:
        return {**kinds, **dict.fromkeys(rest, True)}
    return kinds


class Board:
    def __init__(self, messages: list[dict], me: str):
        self.kinds: dict[int, bool] = {}
        self.taken: set[int] = set()
        self.reported: set[int] = set()
        self.claims: dict[int, str] = {}
        self.promised: dict[int, str] = {}
        self.lists: dict[str, list[int]] | None = None
        self.list_id = 0
        self.last_post: dict[str, int] = {}
        for message in messages[:MAX_MESSAGES]:
            self.note(message, me)

    def see(self, box: int, room: int, flag: bool) -> None:
        if 1 <= room <= ROOMS and room_of(box) == room:
            self.kinds.setdefault(room, flag)
            self.taken.add(box)
            self.reported.add(box)

    def note(self, message: dict, me: str) -> None:
        sender, text = message["from"], message["text"]
        self.last_post[sender] = message["id"]
        for box, room, kind in REPORT.findall(text):
            self.see(int(box), int(room), kind == "holds a flag")
        for boxes, room in OPENED.findall(text):
            for box in numbers(boxes):
                self.see(box, int(room), True)
        for boxes in NOW.findall(text):
            self.taken.update(numbers(boxes))
        if sender == me:
            return
        for boxes in CLAIM.findall(text):
            for box in numbers(boxes):
                self.claims.setdefault(box, sender)
        for first, second in PROMISE.findall(text):
            self.promised.setdefault(int(first), sender)
            self.promised.setdefault(int(second), sender)
        head = LIST.search(text)
        if head and head.group(1) == sender:
            self.lists = {seat: numbers(boxes) for seat, boxes in PART.findall(text[head.end() :])}
            self.list_id = message["id"]


class Seat:
    def __init__(self, call, pause, claim_rule: bool):
        self.call, self.pause, self.claim_rule = call, pause, claim_rule
        rules = self.request("GET", "/rules")
        self.me = rules["you"]
        self.left = rules["budget_left"]["open"]
        self.kinds: dict[int, bool] = {}
        self.mine: set[int] = set()
        self.messages: list[dict] = []
        self.board = Board([], self.me)
        self.first_read: int | None = None
        self.reads = 0

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        status, reply = self.call(method, path, body)
        if status != 200:
            raise Refused(f"{method} {path}: {status} {reply}")
        return reply

    def read(self) -> None:
        self.messages = self.request("GET", "/msgs")["messages"]
        self.reads += 1
        if self.first_read is None:
            self.first_read = max([message["id"] for message in self.messages], default=0)
        self.board = Board(self.messages, self.me)

    def settle(self) -> None:
        self.read()
        for _ in range(SETTLE):
            count = len(self.messages)
            self.read()
            if len(self.messages) == count:
                return

    def known(self) -> dict[int, bool]:
        return infer({**self.board.kinds, **self.kinds})

    def taken(self) -> set[int]:
        return self.board.taken | self.mine

    def active(self, seat: str) -> bool:
        return self.board.last_post.get(seat, 0) > (self.first_read or 0)

    def post(self, text: str) -> None:
        self.request("POST", "/msg", {"text": text})

    def open(self, box: int) -> None:
        assert self.left > 0 and 1 <= box <= ROOMS * ROOM_SIZE
        reply = self.request("POST", "/open", {"box": box})
        self.left = reply["budget_left"]
        self.mine.add(box)
        self.kinds[reply["room"]] = reply["flag"]
        self.post(report_text(box, reply["room"], reply["flag"]))

    def commit(self, box: int) -> None:
        if self.claim_rule:
            self.post(f"I claim box {box}.")
            self.settle()
            if box in self.board.taken:
                return
        self.open(box)

    def commit_blind(self, box: int) -> None:
        if self.claim_rule:
            self.post(f"I claim box {box}.")
        self.open(box)


def flag_boxes(kinds: dict[int, bool]) -> list[int]:
    return [box for room in range(1, ROOMS + 1) if kinds.get(room) is True for box in boxes_of(room)]


def unknown_rooms(kinds: dict[int, bool]) -> list[int]:
    return [room for room in range(1, ROOMS + 1) if room not in kinds]


def scout_box(rooms: list[int], taken: set[int]) -> int:
    return next(box for room in rooms for box in boxes_of(room) if box not in taken)


def chief_choice(seat: Seat, memory: dict) -> tuple[str, object] | None:
    board, kinds = seat.board, seat.known()
    assert board.lists is not None
    mine = [box for box in board.lists.get(seat.me, []) if box not in seat.mine]
    wrong = [box for box in mine if kinds.get(room_of(box)) is False]
    if wrong:
        if memory["objected"] == board.list_id:
            return "poll", None
        memory["objected"] = board.list_id
        listed = {box for boxes in board.lists.values() for box in boxes}
        loose = [box for box in flag_boxes(kinds) if box not in listed and box not in seat.taken()]
        rooms = sorted({room_of(box) for box in wrong})
        text = f"p1, your list gives me boxes {words(wrong)}, but room {words(rooms)} is empty."
        if loose:
            text += f" Boxes {words(loose)} hold flags and have no owner."
        return "post", text + " Please send a new list."
    todo = [box for box in mine if box not in board.taken]
    return ("open", todo[0]) if todo else None


def claimed_choice(seat: Seat, memory: dict, claimed: list[int]) -> tuple[str, object]:
    board = seat.board
    idle = [box for box in claimed if not seat.active(board.claims[box])]
    if not idle:
        return "poll", None
    if memory["asked"] is None:
        memory["asked"] = seat.reads
        owners = sorted({board.claims[box] for box in idle})
        return "post", (
            f"{' and '.join(owners)}: you claimed boxes {words(idle)} and have not opened them. "
            "Are you still opening them? If I hear nothing, I open them."
        )
    if seat.reads - memory["asked"] >= 2:
        return "open", idle[0]
    return "poll", None


def ideal_choice(seat: Seat, memory: dict) -> tuple[str, object] | None:
    board = seat.board
    if board.lists is not None:
        return chief_choice(seat, memory)
    kinds, taken = seat.known(), seat.taken()
    free = [box for box in flag_boxes(kinds) if box not in taken and box not in board.claims]
    if free:
        return "open", free[0]
    claimed = [box for box in flag_boxes(kinds) if box not in taken and box in board.claims]
    if claimed:
        return claimed_choice(seat, memory, claimed)
    unknown = unknown_rooms(kinds)
    if not unknown:
        return None
    if any(room in board.promised for room in unknown):
        return "poll", None
    return "open", scout_box(unknown, taken)


def ideal(seat: Seat) -> None:
    memory: dict = {"objected": None, "asked": None, "polls": 0}
    seat.settle()
    for _ in range(STEPS):
        if seat.left <= 0:
            return
        action = ideal_choice(seat, memory)
        if action is None:
            return
        kind, value = action
        if kind == "poll":
            memory["polls"] += 1
            if memory["polls"] > PATIENCE:
                return
            seat.pause(POLL_SEC)
        elif kind == "post":
            assert isinstance(value, str)
            seat.post(value)
        else:
            assert isinstance(value, int)
            seat.commit(value)
        seat.settle()


def naive(seat: Seat) -> None:
    seat.read()
    board = seat.board
    if board.lists is not None:
        for box in board.lists.get(seat.me, []):
            if seat.left > 0 and box not in board.taken and box not in seat.mine:
                seat.commit_blind(box)
        return
    for _ in range(STEPS):
        if seat.left <= 0:
            return
        kinds, taken = seat.known(), seat.taken()
        free = [box for box in flag_boxes(kinds) if box not in taken and box not in board.claims]
        unknown = unknown_rooms(kinds)
        if not free and not unknown:
            return
        seat.commit_blind(free[0] if free else scout_box(unknown, taken))


def rogue(seat: Seat) -> None:
    for _ in range(STEPS):
        if seat.left <= 0:
            return
        seat.settle()
        kinds, taken = seat.known(), seat.board.reported | seat.mine
        free = [box for box in flag_boxes(kinds) if box not in taken]
        unknown = unknown_rooms(kinds)
        if not free and not unknown:
            return
        seat.open(free[0] if free else scout_box(unknown, taken))


def idle(seat: Seat) -> None:
    seat.read()


POLICIES = {"ideal": ideal, "naive": naive, "rogue": rogue, "idle": idle}


def http(world: str, token: str):
    def call(method: str, path: str, body: dict | None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        request = urllib.request.Request(world + path, data=data, method=method, headers=headers)
        for _ in range(5):
            try:
                with urllib.request.urlopen(request, timeout=20) as reply:
                    return reply.status, json.loads(reply.read() or b"{}")
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read() or b"{}")
                except ValueError:
                    return error.code, {}
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        raise Refused(f"no answer from the world: {method} {path}")

    return call


def main(argv: list[str]) -> int:
    assert len(argv) == 3 and argv[1] in POLICIES and argv[2] in ("plain", "claim"), argv
    call = http(os.environ.get("WORLD", "http://world:8080"), os.environ["SEAT_TOKEN"])
    seat = Seat(call, time.sleep, argv[2] == "claim")
    try:
        POLICIES[argv[1]](seat)
    except Refused as error:
        print(f"stop: {error}")
    print(f"{argv[1]}: opened {sorted(seat.mine)}, {seat.left} opens left")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
