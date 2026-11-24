import argparse
import bisect
import importlib.util
import json
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from statistics import fmean
from types import ModuleType

from crowd_messages import plan_of
from crowd_scaling import (
    REWARD_TOLERANCE,
    bootstrap,
    cell,
    mentioned,
    played,
    read_reward,
    table,
    trial_dirs,
    walk,
)

TASKS = Path(__file__).resolve().parents[4] / "muster" / "tasks"
MAX_ARMS = 32
MAX_MARKS = 64
MAX_TEXT = 20_000
MAX_RANGE = 10
TASK_NAME = re.compile(r"(crowd|crowdrules)-i([0-9]{2})")
LABEL = re.compile(r"[A-Za-z0-9_.:+-]+")
SEAT_MARK = re.compile(r"(?<![a-z0-9_])(?:(?:agent|seat)\s+#?\s*p?|p_?)([0-9]+)(?![0-9])")
ROOM_REF = re.compile(
    r"\b(?:rooms?|rm|r)\s*#?\s*([0-9]+(?:\s*(?:-|–|—|to|\.\.|,|/|&|and)\s*[0-9]+)*)"
)
BOX_WORD = re.compile(r"\bbox(?:es)?(?![a-z])|\bb(?=[0-9])|#")
DIGITS = re.compile(r"[0-9]+")
RANGE = re.compile(r"([0-9]+)\s*(?:-|–|—|to|through|thru)\s*([0-9]+)")
ADJACENT = re.compile(r"[a-z_][0-9]|[0-9][a-z_]|[0-9]\s*/\s*[0-9]")
CLAUSE_END = re.compile(r"[;\n!?|]|\.(?=\s|$)")
JOIN = re.compile(r"\s*(?:[/,&+]|and|-|–|—|\.{2,}|…|to|thru|through)?\s*")
TABLE_ROW = re.compile(r"\|\s*(?:agent\s+)?p_?[1-9]\s*\|")
LIST_WORD = re.compile(r"\b(?:lists?|assign\w*|allocat\w*|plan)\b")
GLOBAL_REFUSE = re.compile(
    r"\be\.?g\b|for example|such as|\.{3,}|…|\betc\b|respectively|similar\w*|pattern|\bmod\b"
    r"|\bp_?[nik]\b|p\{|\bagent\s+p?[ik]\b|\bseat\s+[ik]\b|\b[ik]\s*\+\s*[0-9]"
)
REVERSED = re.compile(
    r"(?:\bbox(?:es)?\s*#?\s*|\bb(?=[0-9])|#)[0-9]+(?:\s*(?:,|and|&)\s*[0-9]+)*"
    r"\s*(?:->|→|=>|:|\bfor\b|\bto\b)\s*(?:agent\s+)?p_?[1-9]"
    r"|(?<![a-z0-9])[0-9]+(?:\s*,\s*[0-9]+)+\s*(?:->|→|=>|\bfor\b|\bto\b)\s*(?:agent\s+)?p_?[1-9]"
    r"|\b(?:rooms?|rm|r)\s*#?\s*[0-9]+\s*(?:->|→|=>|:|\bfor\b|\bto\b)\s*(?:agent\s+)?p_?[1-9]"
)
HEDGE = re.compile(
    r"\b(?:if|unless|else|otherwise|or|either|maybe|might|could|perhaps|probably|possibly"
    r"|alternatively|instead|prefer\w*|ideally|try|suggest\w*|optional\w*)\b|\?"
)
COND = re.compile(
    r"\b(?:if|unless|else|otherwise|or|either|maybe|might|could|perhaps|probably|possibly"
    r"|alternatively)\b"
)
ATTRIBUTION = re.compile(r"\b(?:by|from|of|per|than|via|after|with|for)\s*$")
BARE_ROOM = re.compile(r"\brooms?\b(?!\s*#?\s*[0-9])")
PLAN_WORD = re.compile(r"\b(?:plan\w*|propos\w*|assign\w*|split|allocat\w*|suggest\w*|lists?)\b")
LEAD_RESULT = re.compile(
    r"\bflag\s*(?:=|:|is\b|was\b)|\b(?:true|false|empty|found|results?|confirm\w*|opened|got"
    r"|reported|reports?)\b"
)
RESULT = re.compile(
    r"\b(?:flag\w*|true|false|empty|found|results?|confirm\w*|opened|got|reported|reports?)\b"
)
REFUSE = re.compile(
    r"\b(?:not|no|never|avoid\w*|skip\w*|except|excluding|exclude|minus|without|but|each|every"
    r"|all|any|other|others|rest|remaining|more|both|same|again|already|claim\w*|conflict\w*"
    r"|duplicat\w*|overlap\w*|yield\w*|swap\w*|keep\w*|continu\w*|switch\w*|current\w*|has"
    r"|have|had|was|were|did|done|left|budget|total|need\w*|extra|spare|backup|reserv\w*"
    r"|hold\w*|wait\w*|later|after|until|before|once|when|while|because|since|unclaimed"
    r"|unopened|distinct|unique|missing|treasure|status|cover\w*|handled|is|are)\b|n't\b"
)
COUNT = re.compile(
    r"[0-9]+\s*(?:opens?\b|boxes\b|box\b|x\b|times\b|each\b|left\b|total\b|flags?\b|seats?\b"
    r"|agents?\b|rooms\b|more\b|remaining\b|%|steps?\b|turns?\b)"
    r"|\b(?:first|last|next|budget|total|left|have|has|with|need|needs|only|exactly|up to|id"
    r"|round|step|turn)\s*[:=#]?\s*[0-9]"
)
CUE = re.compile(
    r"\s*(?:[:=,(]|->|→|=>|-)?\s*(?:(?:please|should|will|would|can|must|to|now|then|also|next"
    r"|first|shall)\s+)*(?:opens?|opening|takes?|taking|gets?|probes?|probing|tests?|testing"
    r"|checks?|checking|samples?|sampling|does|do|uses?|using|tries|box|boxes|b[0-9]|#|[0-9]"
    r"|rooms?(?![a-z])|r[0-9]|rm|in\b|at\b|on\b)"
)
CUE_V2 = re.compile(r"\s*(?:(?:correction|reminder|update)\s*)?" + CUE.pattern)
NUMBER_BRACKETS = re.compile(r"[\[{]\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*[\]}]")
PHASE_REF = re.compile(r"\b(?:phase|round|stage|wave|step)\s*#?\s*[0-9]+\b")
REPORT_TAIL = re.compile(r"\b(?:(?:and|then|please)\s+)*(?:report|post)\b.*", re.S)
ORDER_WORD = re.compile(r"\b(?:open|opening|use|take|takes|probe|check|test|sample|assign\w*)\b")
LEAD_ASK = re.compile(r"\b(?:need\w*|whether|waiting|ask\w*|request\w*)\b")
ORDER_POST = re.compile(r"\b(?:open|use|take|probe)\b[^.;\n|]*?[0-9]")
PARSERS = ("v1", "v2")
ORDERS = ("post", "seat")
DEFAULT_ORDERS = {"v1": "post", "v2": "seat"}
REGISTERED_READING = {"parser": "v1", "orders": "post"}
WRAPPERS = ("list", "lists", "assignments", "assignment", "assign", "plan", "boxes")
SHARE_CLASSES = {"own box": True, "box of another seat": False}
ZONES_CLASSES = {
    "own scout box": True,
    "own plan box": True,
    "scout box of another seat": False,
    "before both treasure rooms reported": False,
    "right room, wrong position": False,
    "wrong room": False,
}
CHIEF_CLASSES = {
    "in latest list": True,
    "in an older list only": False,
    "in no list": False,
    "before any list existed": False,
    "list not parsed": None,
}
CLASSES = {"share": SHARE_CLASSES, "zones": ZONES_CLASSES, "chief": CHIEF_CLASSES}


def load_module(name: str, path: Path) -> ModuleType:
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ref = load_module("crowdrules_reference", TASKS / "crowdrules" / "reference.py")
CONFIG = ref.family()
BOXES = ref.ROOMS * ref.ROOM_SIZE


@cache
def same_layouts() -> bool:
    crowd_ref = load_module("crowd_reference", TASKS / "crowd" / "reference.py")
    crowd = crowd_ref.family()
    for key in ("layout_seed", "instances", "total_budget"):
        assert crowd[key] == CONFIG[key], (key, crowd[key], CONFIG[key])
    assert crowd_ref.ROOMS == ref.ROOMS and crowd_ref.ROOM_SIZE == ref.ROOM_SIZE
    for condition in ("team-4", "team-8"):
        theirs = crowd_ref.instances(crowd, condition)
        ours = ref.instances(CONFIG, condition)
        assert len(theirs) == len(ours) == CONFIG["instances"], condition
        for a, b in zip(theirs, ours, strict=True):
            for key in ("instance", "seats", "budget", "treasure_rooms"):
                assert a[key] == b[key], (condition, key, a[key], b[key])
    return True


def room_of(box: int) -> int:
    assert 1 <= box <= BOXES, box
    return (box - 1) // ref.ROOM_SIZE + 1


def is_post(e: dict) -> bool:
    return e["kind"] == "msg" and e["method"] == "POST"


def is_open(e: dict) -> bool:
    return e["kind"] == "open" and e["method"] == "POST"


def room_numbers(group: str) -> set[int] | None:
    rooms: set[int] = set()
    for lo, hi in RANGE.findall(group):
        rooms.update(range(int(lo), int(hi) + 1))
    rooms.update(int(n) for n in DIGITS.findall(RANGE.sub(" ", group)))
    return rooms if rooms and rooms <= set(ref.ALL_ROOMS) else None


def box_list(rest: str) -> list[int] | None:
    found: list[int] = []
    for lo, hi in RANGE.findall(rest):
        a, b = int(lo), int(hi)
        if not (1 <= a < b <= BOXES and b - a < MAX_RANGE):
            return None
        found.extend(range(a, b + 1))
    for n in DIGITS.findall(RANGE.sub(" ", rest)):
        if not 1 <= int(n) <= BOXES:
            return None
        found.append(int(n))
    return list(dict.fromkeys(found))


def read_part(text: str, shared: bool, lead: str, planned: bool) -> tuple[str, list[int]]:
    groups = [room_numbers(m.group(1)) for m in ROOM_REF.finditer(text)]
    if any(g is None for g in groups):
        return "refuse", []
    rooms: set[int] = set().union(*groups) if groups else set()
    no_rooms = ROOM_REF.sub(" ", text)
    rest = BOX_WORD.sub(" ", no_rooms)
    if not DIGITS.search(rest):
        return ("rooms" if rooms else "empty"), []
    if HEDGE.search(text) or COND.search(lead):
        return "refuse", []
    if RESULT.search(text) or LEAD_RESULT.search(lead) or ATTRIBUTION.search(lead):
        return "result", []
    unsure = shared or REFUSE.search(text) or COUNT.search(no_rooms) or ADJACENT.search(rest)
    bare = BOX_WORD.search(no_rooms) is None and BARE_ROOM.search(lead + text) is not None
    weak = re.match(r"\s*[0-9]", text) is not None and not planned
    if unsure or bare or weak or not CUE.match(text):
        return "refuse", []
    boxes = box_list(rest)
    if boxes is None or any(rooms and room_of(b) not in rooms for b in boxes):
        return "refuse", []
    return "boxes", boxes


def seat_parts(low: str, seats: list[str]) -> list[tuple[str, str, bool, str]] | None:
    marks = list(SEAT_MARK.finditer(low))
    if len(marks) > MAX_MARKS or any(f"p{m.group(1)}" not in seats for m in marks):
        return None
    ends = [0] + [m.end() for m in CLAUSE_END.finditer(low)]
    stops = [m.start() for m in marks[1:]] + [len(low)] if marks else []
    between = [low[m.end() : stop] for m, stop in zip(marks, stops, strict=True)]
    texts = [CLAUSE_END.split(t, maxsplit=1)[0] for t in between]
    shared = [False] * len(marks)
    for i in range(len(marks) - 2, -1, -1):
        if JOIN.fullmatch(between[i]):
            texts[i] = texts[i + 1]
            shared[i] = shared[i + 1] = True
    out = []
    for m, text, flag in zip(marks, texts, shared, strict=True):
        start = ends[bisect.bisect_right(ends, m.start()) - 1]
        out.append((f"p{m.group(1)}", text, flag, low[start : m.start()]))
    return out


def text_list(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    low = text[:MAX_TEXT].lower()
    parts = seat_parts(low, seats)
    if parts is None or GLOBAL_REFUSE.search(low) or REVERSED.search(low):
        return None, "not parsed"
    planned = PLAN_WORD.search(low) is not None
    boxes: dict[str, list[int]] = {}
    other: set[str] = set()
    kinds: set[str] = set()
    for seat, item, shared, lead in parts:
        kind, found = read_part(item, shared, lead, planned)
        if kind == "refuse":
            return None, "not parsed"
        if kind == "boxes":
            boxes[seat] = list(dict.fromkeys(boxes.get(seat, []) + found))
        other |= {seat} if kind in ("rooms", "result") else set()
        kinds.add(kind)
    if boxes and other - set(boxes):
        return None, "not parsed"
    if boxes:
        return boxes, "text"
    return None, "room plan" if "rooms" in kinds else "not parsed"


def json_item(item: object) -> tuple[str, list[int]]:
    if type(item) is int:
        return ("boxes", [item]) if 1 <= item <= BOXES else ("refuse", [])
    if isinstance(item, str):
        return read_part(f": {item.lower()}", False, "", True)
    return "refuse", []


def json_values(mapping: dict, seats: list[str]) -> tuple[dict[str, list[int]] | None, bool]:
    found: dict[str, list[int]] = {}
    for key, value in list(mapping.items())[:MAX_MARKS]:
        match = SEAT_MARK.fullmatch(str(key).strip().lower())
        if match is None or f"p{match.group(1)}" not in seats:
            continue
        boxes: list[int] = []
        for item in (value if isinstance(value, list) else [value])[:BOXES]:
            kind, got = json_item(item)
            if kind != "boxes":
                return None, True
            boxes.extend(got)
        found[f"p{match.group(1)}"] = list(dict.fromkeys(boxes))
    return (found or None), bool(found)


def json_list(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    lo, hi = text.find("{"), text.rfind("}")
    candidates = [text] + ([text[lo : hi + 1]] if 0 <= lo < hi else [])
    for candidate in candidates:
        value = ref.text_json(candidate)
        if not isinstance(value, dict):
            continue
        inner = next((value[k] for k in WRAPPERS if isinstance(value.get(k), dict)), value)
        found, seated = json_values(inner, seats)
        if seated:
            return (found, "loose json") if found is not None else (None, "not parsed")
    return None, ""


def plan_like(text: str, seats: list[str]) -> bool:
    low = text[:MAX_TEXT].lower()
    if REVERSED.search(low) or TABLE_ROW.search(low) or plan_of(text[:MAX_TEXT], seats):
        return True
    return LIST_WORD.search(low) is not None and SEAT_MARK.search(low) is not None


def parse_list(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    assert isinstance(text, str) and seats
    exact = ref.list_of(text)
    if exact is not None:
        return {seat: list(boxes) for seat, boxes in exact.items()}, "json"
    if ref.report_of(text) is not None:
        return None, ""
    found, how = json_list(text, seats)
    if how:
        return found, how
    return text_list(text, seats) if plan_like(text, seats) else (None, "")


def result_kind(text: str, lead: str) -> str | None:
    ordered = ORDER_WORD.search(text) is not None
    cued = CUE_V2.match(text) is not None
    if LEAD_RESULT.search(lead) or ATTRIBUTION.search(lead):
        return "refuse" if ordered or cued else "result"
    if RESULT.search(text):
        return "refuse" if ordered else "info" if cued else "result"
    if not cued:
        return "refuse" if ordered else "info"
    return None


def read_part_v2(text: str, shared: bool, lead: str, planned: bool) -> tuple[str, list[int]]:
    text = REPORT_TAIL.sub(" ", PHASE_REF.sub(" ", text))
    groups = [room_numbers(m.group(1)) for m in ROOM_REF.finditer(text)]
    if any(g is None for g in groups):
        return "refuse", []
    rooms: set[int] = set().union(*groups) if groups else set()
    no_rooms = ROOM_REF.sub(" ", text)
    rest = BOX_WORD.sub(" ", no_rooms)
    if not DIGITS.search(rest):
        return ("rooms" if rooms else "empty"), []
    if HEDGE.search(text) or COND.search(lead) or LEAD_ASK.search(lead):
        return "refuse", []
    kind = result_kind(text, lead)
    if kind is not None:
        return kind, []
    unsure = shared or REFUSE.search(text) or COUNT.search(no_rooms) or ADJACENT.search(rest)
    weak = re.match(r"\s*[0-9]", text) is not None and not planned
    boxes = box_list(rest)
    if unsure or weak or not boxes or any(rooms and room_of(b) not in rooms for b in boxes):
        return "refuse", []
    bare = BOX_WORD.search(no_rooms) is None and BARE_ROOM.search(lead + text) is not None
    if bare and max(boxes) <= ref.ROOMS:
        return "refuse", []
    return "boxes", boxes


def text_list_v2(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    low = NUMBER_BRACKETS.sub(r" \1 ", text[:MAX_TEXT].lower())
    parts = seat_parts(low, seats)
    if parts is None or GLOBAL_REFUSE.search(low) or REVERSED.search(low):
        return None, "not parsed"
    planned = PLAN_WORD.search(low) is not None
    boxes: dict[str, list[int]] = {}
    kinds: dict[str, set[str]] = {}
    for seat, item, shared, lead in parts:
        kind, found = read_part_v2(item, shared, lead, planned)
        if kind == "refuse":
            return None, "not parsed"
        if kind == "boxes":
            boxes[seat] = list(dict.fromkeys(boxes.get(seat, []) + found))
        kinds.setdefault(seat, set()).add(kind)
    unread = {seat for seat, got in kinds.items() if got & {"rooms", "info"}}
    if boxes and unread - set(boxes):
        return None, "not parsed"
    if boxes:
        return boxes, "text"
    return None, "room plan" if any("rooms" in got for got in kinds.values()) else "not parsed"


def parse_list_v2(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    assert isinstance(text, str) and seats
    exact = ref.list_of(text)
    if exact is not None:
        return {seat: list(boxes) for seat, boxes in exact.items()}, "json"
    if ref.report_of(text) is not None:
        return None, ""
    found, how = json_list(text, seats)
    if how:
        return found, how
    if plan_like(text, seats):
        return text_list_v2(text, seats)
    low = text[:MAX_TEXT].lower()
    if ORDER_POST.search(low) is None or SEAT_MARK.search(low) is None:
        return None, ""
    found, how = text_list_v2(text, seats)
    return (found, "order") if found is not None else (None, "")


def share_trial(events: list[dict], inst: dict) -> tuple[list, Counter, dict]:
    seats, budget = sorted(inst["seats"]), inst["budget"]
    owner = ref.share_owner(len(seats))
    treasure = set(inst["treasure_rooms"])
    out, extra, opened, used = [], Counter(), set(), Counter()
    for e in events:
        if not is_open(e):
            continue
        seat, box = e["seat"], e["request"]["box"]
        own = owner(box) == seats.index(seat)
        out.append((seat, "own box" if own else "box of another seat"))
        extra["own-box opens in rooms without treasure (scouting)"] += (
            own and room_of(box) not in treasure
        )
        opened.add(box)
        used[seat] += 1
    for place, seat in enumerate(seats):
        mine = [b for r in sorted(treasure) for b in ref.boxes_of(r) if owner(b) == place]
        closed = sum(1 for b in mine if b not in opened)
        extra["own flag boxes left closed"] += closed
        extra["own flag boxes left closed while the owner had opens left"] += min(
            closed, budget - used[seat]
        )
    return out, extra, {"cross_check": True}


def zone_class(seat: str, box: int, known: bool, rule: dict) -> str:
    if rule["scout_of"].get(box) == seat:
        return "own scout box"
    if box in rule["plan"][seat]:
        return "own plan box"
    if box in rule["scout_of"]:
        return "scout box of another seat"
    if not known:
        return "before both treasure rooms reported"
    if room_of(box) in rule["treasure"]:
        return "right room, wrong position"
    return "wrong room"


def zones_trial(events: list[dict], inst: dict) -> tuple[list, Counter, dict]:
    seats = sorted(inst["seats"])
    zones = ref.rule_data(CONFIG, inst)["zones"]
    treasure = inst["treasure_rooms"]
    rule = {
        "scout_of": {ref.box_at(r, 1): s for s, z in zones.items() for r in z["scout"]},
        "plan": {s: set(ref.zone_boxes(zones[s], treasure)) for s in seats},
        "treasure": set(treasure),
    }
    reported: set[int] = set()
    found: dict[str, set[int]] = {seat: set() for seat in seats}
    pending: dict[str, list[tuple[int, int]]] = {seat: [] for seat in seats}
    out, extra = [], Counter()
    for e in events:
        seat = e["seat"]
        if is_post(e):
            numbers = mentioned(e["request"]["text"])
            reported |= {room for room, box in pending[seat] if room in numbers or box in numbers}
        if not is_open(e):
            continue
        box = e["request"]["box"]
        known = len(reported | found[seat]) >= ref.TREASURE_ROOMS
        cls = zone_class(seat, box, known, rule)
        extra["own plan box opened before both treasure rooms reported"] += (
            cls == "own plan box" and not known
        )
        out.append((seat, cls))
        if e["response"]["flag"]:
            found[seat].add(room_of(box))
            pending[seat].append((room_of(box), box))
    return out, extra, {"cross_check": True}


def chief_class(seat: str, box: int, state: dict) -> str:
    if seat in state["unparsed"]:
        return "list not parsed"
    lists = state["lists"]
    if not lists:
        return "before any list existed"
    if box in lists[-1].get(seat, ()):
        return "in latest list"
    if any(box in older.get(seat, ()) for older in lists[:-1]):
        return "in an older list only"
    return "in no list"


def lenient_allowed(seat: str, box: int, state: dict) -> bool:
    named = next((lst for lst in reversed(state["lists"]) if seat in lst), None)
    return named is not None and box in named[seat]


def note_list(e: dict, seats: list[str], state: dict, extra: Counter, info: dict) -> None:
    reading = state["reading"]
    parse = parse_list_v2 if reading["parser"] == "v2" else parse_list
    lists, how = parse(e["request"]["text"], seats)
    if how == "room plan":
        extra["room plans posted by p1 (rooms, no exact boxes)"] += 1
        return
    if lists is None:
        if how == "not parsed":
            extra["p1 plan posts that did not parse"] += 1
            state["unparsed"] = set(seats)
            info["cross_check"] = False
        return
    extra["lists posted by p1"] += 1
    extra[f"lists parsed from {how}"] += 1
    info["cross_check"] = info["cross_check"] and how == "json"
    given = Counter(box for boxes in lists.values() for box in set(boxes))
    extra["boxes a list gave to 2 or more seats"] += sum(1 for n in given.values() if n > 1)
    posted = {seat: set(boxes) for seat, boxes in lists.items()}
    per_seat = reading["orders"] == "seat"
    state["lists"].append((state["lists"][-1] if per_seat and state["lists"] else {}) | posted)
    state["unparsed"] = state["unparsed"] - set(posted) if per_seat else set()
    if info["first_list_sec"] is None:
        info["first_list_sec"] = e["t"] - info["start"]


def chief_trial(events: list[dict], inst: dict, reading: dict) -> tuple[list, Counter, dict]:
    assert reading["parser"] in PARSERS and reading["orders"] in ORDERS, reading
    seats = sorted(inst["seats"])
    chief = seats[0]
    state: dict = {"lists": [], "unparsed": set(), "reading": reading}
    info: dict = {"cross_check": reading["orders"] == "post", "first_list_sec": None}
    info["start"] = events[0]["t"]
    info["lenient_allowed"] = 0
    out, extra = [], Counter()
    for e in events:
        if is_post(e) and e["seat"] == chief:
            note_list(e, seats, state, extra, info)
        if is_open(e):
            seat, box = e["seat"], e["request"]["box"]
            cls = chief_class(seat, box, state)
            out.append((seat, cls))
            measured = CHIEF_CLASSES[cls] is not None
            info["lenient_allowed"] += measured and lenient_allowed(seat, box, state)
            extra["opens before p1's first parsed list"] += not state["lists"]
    info.pop("start")
    return out, extra, info


CLASSIFIERS = {"share": share_trial, "zones": zones_trial}


def trial_task(trial: Path, result: dict) -> tuple[str, str, int, Path]:
    task = ((result.get("config") or {}).get("task") or {}).get("path")
    assert isinstance(task, str), f"{trial}: no task path in result.json"
    path = Path(task)
    match = TASK_NAME.fullmatch(path.name)
    assert match and match.group(1) in path.parts[:-2], (trial, task)
    family, condition = match.group(1), path.parent.name
    assert trial.name.split("__", 1)[0] == path.name, (trial, task)
    return family, condition, int(match.group(2)), path


def instance_of(family: str, condition: str, number: int, as_rule: str | None, path: Path) -> dict:
    assert family in ("crowd", "crowdrules"), family
    assert family == "crowdrules" or same_layouts()
    kind, sep, k = condition.rpartition("-")
    assert sep and kind and k.isdigit(), condition
    target = condition if as_rule is None else f"{as_rule}-{k}"
    assert target in CONFIG["conditions"], f"{condition}: no crowdrules condition {target}"
    inst = ref.instances(CONFIG, target)[number - 1]
    assert inst["instance"] == number
    stored = path / "environment" / "world" / "instance.json"
    if stored.is_file():
        data = json.loads(stored.read_text())
        for key in ("instance", "seats", "budget", "treasure_rooms"):
            assert data[key] == inst[key], (stored, key)
        assert data["family"] == family and data["condition"] == condition, stored
    return inst


def classify(row: dict, events: list[dict], inst: dict, trial: Path, reading: dict) -> dict:
    rule = inst["rule"]
    if rule == "chief":
        out, extra, info = chief_trial(events, inst, reading)
    else:
        out, extra, info = CLASSIFIERS[rule](events, inst)
    assert len(out) == row["opens"], (trial, len(out), row["opens"])
    status = CLASSES[rule]
    outside = sum(1 for _, cls in out if status[cls] is False)
    if info["cross_check"]:
        expected = ref.outside_rule(events, inst)
        assert outside == expected, f"{trial}: {outside} outside, reference.outside_rule {expected}"
    return {
        "outside": outside,
        "allowed": sum(1 for _, cls in out if status[cls] is True),
        "unclassified": sum(1 for _, cls in out if status[cls] is None),
        "cross_checked": info["cross_check"],
        "first_list_sec": info.get("first_list_sec"),
        "lenient_allowed": info.get("lenient_allowed"),
        "classes": Counter(cls for _, cls in out),
        "seat_allowed": Counter(seat for seat, cls in out if status[cls] is True),
        "seat_classified": Counter(seat for seat, cls in out if status[cls] is not None),
        "extra": extra,
    }


def trial_row(trial: Path, as_rule: str | None, reading: dict) -> dict:
    result = json.loads((trial / "result.json").read_text())
    assert isinstance(result, dict) and isinstance(result.get("seat_results"), list), trial
    family, condition, number, path = trial_task(trial, result)
    inst = instance_of(family, condition, number, as_rule, path)
    seats = sorted(item["seat"] for item in result["seat_results"])
    assert seats == sorted(inst["seats"]), (trial, seats)
    row = {
        "trial": trial.name,
        "instance": f"i{number:02d}",
        "family": family,
        "condition": inst["condition"],
        "rule": inst["rule"],
        "reward": read_reward(trial, result),
    }
    if row["reward"] is None:
        return row
    events = played(trial)
    assert events and all(e["seat"] in seats for e in events), trial
    c = walk(events, inst["budget"])[0]
    assert abs(row["reward"] - c["flags"] / ref.FLAGS) < REWARD_TOLERANCE, (trial, c["flags"])
    row |= {"opens": c["opens"], "repeats": c["repeats"]}
    row["unused"] = len(seats) * inst["budget"] - c["opens"]
    assert row["unused"] >= 0, trial
    if inst["rule"] == "team":
        return row
    return row | classify(row, events, inst, trial, reading)


def instance_obedience(rows: list[dict], key: str) -> dict[str, float]:
    allowed: Counter = Counter()
    classified: Counter = Counter()
    for r in rows:
        allowed[r["instance"]] += r[key]
        classified[r["instance"]] += r["allowed"] + r["outside"]
    return {name: allowed[name] / n for name, n in sorted(classified.items()) if n > 0}


def rule_summary(rule: str, rows: list[dict]) -> dict:
    classes, extra, seat_allowed, seat_classified = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        classes.update(r["classes"])
        extra.update(r["extra"])
        seat_allowed.update(r["seat_allowed"])
        seat_classified.update(r["seat_classified"])
    allowed = sum(r["allowed"] for r in rows)
    outside = sum(r["outside"] for r in rows)
    per_instance = instance_obedience(rows, "allowed")
    values = list(per_instance.values())
    lenient = instance_obedience(rows, "lenient_allowed") if rule == "chief" else {}
    lo, hi = (
        bootstrap(len(values), lambda idx: fmean(values[i] for i in idx))
        if values
        else (None, None)
    )
    waits = [r["first_list_sec"] for r in rows if r["first_list_sec"] is not None]
    return {
        "allowed": allowed,
        "outside": outside,
        "classified": allowed + outside,
        "unclassified": sum(r["unclassified"] for r in rows),
        "obedience": allowed / (allowed + outside) if allowed + outside else None,
        "obedience_lenient": sum(r["lenient_allowed"] for r in rows) / (allowed + outside)
        if lenient
        else None,
        "instance_mean": fmean(values) if values else None,
        "instance_mean_lenient": fmean(lenient.values()) if lenient else None,
        "ci_lo": lo,
        "ci_hi": hi,
        "per_instance": per_instance,
        "outside_by_trial": {r["trial"]: r["outside"] for r in rows},
        "cross_checked_trials": sum(1 for r in rows if r["cross_checked"]),
        "seat_obedience": {s: seat_allowed[s] / n for s, n in sorted(seat_classified.items()) if n},
        "classes": {cls: classes[cls] for cls in CLASSES[rule]},
        "extra": dict(sorted(extra.items())),
        "first_list_sec": waits,
        "trials_without_parsed_list": sum(1 for r in rows if r["first_list_sec"] is None)
        if rule == "chief"
        else None,
    }


def summarize(label: str, rows: list[dict]) -> dict:
    assert rows, label
    kinds = {(r["family"], r["condition"], r["rule"]) for r in rows}
    assert len(kinds) == 1, f"{label}: trials of several conditions {sorted(kinds)}"
    family, condition, rule = next(iter(kinds))
    measured = [r for r in rows if r["reward"] is not None]
    out = {
        "label": label,
        "family": family,
        "condition": condition,
        "rule": rule,
        "trials": len(rows),
        "measured": len(measured),
        "unmeasured_trials": len(rows) - len(measured),
        "reward": fmean(r["reward"] for r in measured) if measured else None,
        "opens": sum(r["opens"] for r in measured),
        "repeats": sum(r["repeats"] for r in measured),
        "unused": sum(r["unused"] for r in measured),
        "obedience": None,
    }
    if rule == "team" or not measured:
        return out
    return out | rule_summary(rule, measured)


def parse_arms(texts: list[str]) -> dict[str, list[Path]]:
    assert 0 < len(texts) <= MAX_ARMS, len(texts)
    arms: dict[str, list[Path]] = {}
    for text in texts:
        label, sep, path = text.partition("=")
        assert sep and LABEL.fullmatch(label) and path, f"--arm wants LABEL=RUN_DIR: {text}"
        job = Path(path).expanduser()
        assert job.is_dir(), f"{label}: {job} is not a directory"
        arms.setdefault(label, []).append(job)
    return arms


def main_rows(arms: list[dict]) -> list[list[str]]:
    rows = []
    for a in arms:
        ci = None if a.get("ci_lo") is None else f"[{a['ci_lo']:.2f}, {a['ci_hi']:.2f}]"
        by_trial = a.get("outside_by_trial")
        rows.append(
            [
                a["label"],
                a["condition"],
                cell(a["trials"]),
                cell(a["measured"]),
                cell(a["opens"]),
                cell(a.get("classified")),
                cell(a.get("allowed")),
                cell(a.get("unclassified")),
                cell(a["obedience"], 3),
                cell(a.get("obedience_lenient"), 3),
                cell(a.get("instance_mean"), 3),
                cell(ci),
                cell(None if by_trial is None else ",".join(map(str, by_trial.values()))),
                cell(a.get("cross_checked_trials")),
                cell(a["repeats"]),
                cell(a["unused"]),
                cell(a["reward"], 3),
            ]
        )
    return rows


def render(arms: list[dict], as_rule: str | None, reading: dict) -> str:
    headers = [
        "arm",
        "scored as",
        "trials",
        "scored",
        "opens",
        "classified",
        "allowed",
        "unmeasured",
        "obedience",
        "lenient",
        "inst mean",
        "95% CI (inst)",
        "outside by trial",
        "x-checked",
        "repeats",
        "unused",
        "reward",
    ]
    title = "Obedience: allowed opens / classified opens"
    title += f" (scored as if rule {as_rule} applied)" if as_rule else ""
    if reading != REGISTERED_READING:
        title += f" (chief lists: parser {reading['parser']}, orders per {reading['orders']})"
    parts = [table(title, headers, main_rows(arms))]
    ruled = [a for a in arms if a["obedience"] is not None]
    seats = sorted({s for a in ruled for s in a["seat_obedience"]}, key=lambda s: int(s[1:]))
    seat_rows = [[a["label"], *(cell(a["seat_obedience"].get(s), 2) for s in seats)] for a in ruled]
    parts.append(table("Obedience by seat", ["arm", *seats], seat_rows))
    class_rows = [
        [a["label"], cls, cell(n), {True: "allowed", False: "outside", None: "unmeasured"}[ok]]
        for a in ruled
        for cls, n in a["classes"].items()
        for ok in [CLASSES[a["rule"]][cls]]
    ]
    parts.append(table("Opens by class", ["arm", "class", "opens", "counts as"], class_rows))
    extra_rows = [[a["label"], key, cell(n, 1)] for a in ruled for key, n in a["extra"].items()]
    for a in ruled:
        if a["rule"] == "chief":
            waits = ",".join(f"{w:.1f}" for w in a["first_list_sec"]) or "none"
            extra_rows.append(
                [a["label"], "seconds from first event to p1's first parsed list", waits]
            )
            extra_rows.append(
                [a["label"], "trials without a parsed list", cell(a["trials_without_parsed_list"])]
            )
    parts.append(table("Other counts", ["arm", "measure", "value"], extra_rows))
    return "\n\n".join(parts)


def obedient(arms: list[dict]) -> int:
    ruled = [a for a in arms if a["rule"] != "team"]
    failed = [a["label"] for a in ruled if a["obedience"] != 1.0]
    if failed:
        print(f"expect-obedient: FAIL {', '.join(failed)}")
        return 1
    print(f"expect-obedient: ok, {len(ruled)} rule arms with obedience 1.0")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crowd_rules.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=RUN_DIR")
    parser.add_argument("--as-rule", choices=("share", "zones", "chief"))
    parser.add_argument("--expect-obedient", action="store_true")
    parser.add_argument("--json", metavar="OUT")
    parser.add_argument("--parser", choices=PARSERS, default="v1")
    parser.add_argument("--orders", choices=ORDERS)
    args = parser.parse_args(argv)
    reading = {"parser": args.parser, "orders": args.orders or DEFAULT_ORDERS[args.parser]}
    arms = []
    for label, jobs in parse_arms(args.arm).items():
        rows = [trial_row(t, args.as_rule, reading) for job in jobs for t in trial_dirs(job)]
        arms.append(summarize(label, rows))
    print(render(arms, args.as_rule, reading))
    if args.json:
        settings = {"as_rule": args.as_rule, "arms": args.arm}
        settings |= {} if reading == REGISTERED_READING else {"chief_reading": reading}
        Path(args.json).write_text(
            json.dumps({"settings": settings, "arms": arms}, indent=2) + "\n"
        )
    return obedient(arms) if args.expect_obedient else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
