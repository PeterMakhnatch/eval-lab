import argparse
import bisect
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean, median

from crowd_scaling import agent_steps, cell, parse_arm, played, read_reward, table, trial_dirs

ROOM_SIZE = 10
ROOMS = 6
BOXES = ROOM_SIZE * ROOMS
MAX_MESSAGES = 10_000
MAX_STEPS = 10_000
MAX_TOKENS = 1_000
MAX_SEGMENT_PASSES = 16
QUOTE_CHARS = 200
SHORT_FINAL_TEXT = 80
AUDIT_SEED = 0
CHANNEL_KINDS = ("team", "claim")
ROOM_FIRST = frozenset(r * ROOM_SIZE + 1 for r in range(ROOMS))
SEAT = re.compile(r"\bp_?([1-9])\b")
SEAT_WORD = re.compile(r"\b(?:agent|seat)s?\s*#?\s*[1-9]\b")
ROOM_LIST = re.compile(
    r"\b(?:rooms?|r)\s*#?\s*([1-6](?:(?:\s*(?:,|/|&|\+|-|–|to|and|or)\s*)+[1-6](?![0-9]))*)(?![0-9])"
)
ROOM_TOKEN = re.compile(r"room§([1-6]+)§")
RANGE = re.compile(
    r"(?<![0-9])([1-9][0-9]?)\s*(?:-|–|to|through|thru|\.\.)\s*([1-9][0-9]?)(?![0-9])"
)
NUM = re.compile(r"(?:(?<=box)|(?<=boxes)|(?<=#)|(?<![a-z0-9_=.$]))([0-9]{1,2})(?![0-9a-z%])")
BOX_BEFORE = re.compile(r"box(?:es)?\s*#?\s*$")
COUNT_AFTER = re.compile(
    r"\s*(?:x\b|\*|opens?\b|left\b|agents?\b|seats?\b|flags\b|boxes\b|rooms\b|treasure\b|of\b"
    r"|times\b|more\b|remaining\b|each\b|total\b|steps?\b|distinct\b|unopened\b|unclaimed\b"
    r"|guaranteed\b|needed\b|candidates?\b|probes?\b|per\b|successful\b|other\b|new\b"
    r"|additional\b|unused\b|pts\b|points\b|/\s*(?:20|24|60)\b|%)"
)
COUNT_BEFORE = re.compile(
    r"(?:\b(?:budget(?:_left)?|left|have|has|had|exactly|only|with|need|needs|total|step|id|x|k|n)"
    r"|=|\*)\s*[:=]?\s*$"
)
FORMULA = re.compile(r"\bp_?[ik]\b|p\{[ik]\}|\bagent\s+p?[ik]\b|\bseat\s+[ik]\b")
CLAUSE_END = re.compile(r"[.;\n!?|]")
GROUP_GLUE = re.compile(r"\s*(?:[/,&+]|and|or)?\s*")
NOT_ASSIGN = re.compile(
    r"\b(?:results?|report(?:s|ed)?|found|opened|was|were|had|empty|false|true|confirmed|already"
    r"|claimed|claims?|avoid|not|don't|never|did|got|flag=|has|is|are|since|because|sees|switch\w*"
    r"|cannot|can't|exhausted|done)\b"
)
ATTRIBUTION = re.compile(r"(?:\bby|\bfrom|\bof|\bper|\bthan|\bwith|\bfor)\s*$")
ASSIGN_VERB = re.compile(
    r"->|→|[:=]|\b(?:opens?|takes?|probes?|tests?|checks?|covers?|does|do|handles?|gets?|scouts?"
    r"|samples?)\b"
)
COUNT_IN_PARENS = re.compile(r"[0-9]\s*-\s*[0-9]+\s*\($")
TARGET_LEAD = re.compile(r"^\s*(?:=|:|->|→)")
ROOM_WORD = re.compile(r"\brooms?\b")
BARE_NUMBER = re.compile(r"(?<![0-9])[0-9]{1,2}(?![0-9])")
RESERVE = re.compile(
    r"\b(?:hold|wait|waits|reserve|reserved|later|standby|stand by|backup|spare|remaining|after"
    r"|until|fill)\b"
)
PIECE_SPLIT = re.compile(r"[.;\n!?:,()\[\]]|\band\b|\bbut\b|\bwhile\b|\bwhereas\b")
EMPTY = re.compile(
    r"\bempty\b|\bfalse\b|\bno flags?\b|\bnot (?:a )?treasure\b|\bno treasure\b"
    r"|\bflag\s*[=:]\s*(?:no|0)\b"
)
HEDGE = re.compile(
    r"\bif\b|\bunless\b|\bwhether\b|\bmaybe\b|\bmight\b|\bprobabl|\bpossibl|\bnot empty\b|\bexpect"
    r"|\bassum|\bwould\b|\bcould\b|\?|\btrue\b|\bplease\b|\breport\b|\bprobe\b"
    r"|flag\s*/\s*empty|empty\s*/\s*(?:flag|true|false)"
)
CLAIM_VERB = re.compile(
    r"\bwill (?:now |then |also )?(?:open|test|probe|take|use|try|check)\b"
    r"|\bi'?ll (?:open|take|test|probe)\b|\bopening\b|\bclaim(?:s|ing)?\b|\btak(?:e|es|ing)\b"
    r"|\breserv(?:e|es|ing)\b|\b(?:final|next|last) open\b|\bplan\w* (?:to open|box)|\bintend\w*\b"
    r"|\bgoing to open\b|\bopens? (?:box|#)|\bpropos\w* (?:box|open\w*)|\bswitch\w* (?:\w+ ){0,4}?to\b"
    r"|\bnow (?:opens?|probes?|tests?)\b|\bprobes? (?:room ?[1-6] )?(?:with )?box|\busing\b|\bwill be box"
)
NEG_CUT = re.compile(r"\bnot\b|n't\b|\bavoid|\bwithdraw|\byield|\bcancel|\binstead of\b|\bexcept\b")
CLAIM_CUT = re.compile(
    NEG_CUT.pattern + r"|\bsince\b|\bbecause\b|\balready\b|\bwas\b|\bwere\b|\bunless\b|\bif\b"
)
SEAT_ATTRIBUTION = re.compile(
    r"\b(?:after|by|from|per|than|for|with|to) p_?[1-9](?:\s*(?:/|,|and)\s*p_?[1-9])*\b"
)
PURPOSE = re.compile(
    r"\([^)]*\b(?:was|were|by|already|claimed|opened|reported|flag)\b[^)]*\)"
    r"|\bto avoid [^,.;:]*|\bwill avoid duplicates?\b"
    r"|\bavoid(?:s|ing)? (?:duplicates?|conflicts?|collisions?)\b"
)
WAIT_TEXT = re.compile(r"\bwait(?:ing)?\b|\buntil (?:others|other agents|more)\b|\bpending\b")
CLAIM_TEXT = re.compile(
    r"claim|posted first|first (?:posted|mentioned|announced)|already (?:been )?(?:mentioned|posted"
    r"|announced|reserved|taken)|reserved|announced|violat"
)
USELESS_TEXT = re.compile(
    r"no (?:further |more |other )?(?:useful|unclaimed|valid|safe|remaining|known|new|unopened)"
    r"|nothing (?:useful|left|remains|more)|would (?:only )?(?:duplicate|not (?:improve|increase"
    r"|add|help|change))|cannot (?:discover|improve|increase|add)|no (?:new|additional)"
    r"|duplicate|both treasure rooms|all (?:\d+ )?flags|not (?:improve|increase|add)|unnecessary"
)
REASONS = (
    "error/time",
    "turn cap",
    "claim text",
    "nothing useful, true",
    "nothing useful, false",
    "waiting",
    "other",
)
REPEAT_KINDS = ("own", "assign", "simul", "unreported", "unread", "read")


def norm(text: str) -> str:
    assert isinstance(text, str) and len(text) <= 100_000
    return text.lower()


def room_set(group: str) -> set[int]:
    tokens = re.findall(r"[1-6]|-|–|to", group)
    assert len(tokens) <= MAX_TOKENS
    out: set[int] = set()
    pending = False
    for token in tokens:
        if token in ("-", "–", "to"):
            pending = bool(out)
            continue
        value = int(token)
        if pending:
            out.update(range(max(out) + 1, value + 1))
        out.add(value)
        pending = False
    assert all(1 <= r <= ROOMS for r in out), (group, out)
    return out


def rooms_in(low: str) -> set[int]:
    out: set[int] = set()
    for m in ROOM_LIST.finditer(low):
        out |= room_set(m.group(1))
    return out


def strip_refs(low: str) -> str:
    return SEAT_WORD.sub(" ", SEAT.sub(" ", ROOM_LIST.sub(" ", low)))


def counted(s: str, m: re.Match) -> bool:
    before = s[max(0, m.start() - 14) : m.start()]
    after = s[m.end() : m.end() + 16]
    if before.endswith("#") or BOX_BEFORE.search(before):
        return False
    if before.endswith("(") and after.startswith(")"):
        return bool(COUNT_IN_PARENS.search(before))
    return bool(COUNT_AFTER.match(after) or COUNT_BEFORE.search(before))


def boxes_in(low: str) -> set[int]:
    s = strip_refs(low)
    out: set[int] = set()
    for m in RANGE.finditer(s):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a < b <= BOXES and b - a < 2 * ROOM_SIZE:
            out.update(range(a, b + 1))
    for m in NUM.finditer(s):
        value = int(m.group(1))
        if 1 <= value <= BOXES and not counted(s, m):
            out.add(value)
    return out


def room_boxes(rooms: set[int]) -> set[int]:
    return {b for r in rooms for b in range((r - 1) * ROOM_SIZE + 1, r * ROOM_SIZE + 1)}


def room_of(box: int) -> int:
    assert 1 <= box <= BOXES, box
    return (box - 1) // ROOM_SIZE + 1


def room_token(m: re.Match) -> str:
    return f" room§{''.join(map(str, sorted(room_set(m.group(1)))))}§ "


def piece_rooms(piece: str) -> set[int]:
    rooms = {int(d) for m in ROOM_TOKEN.finditer(piece) for d in m.group(1)}
    return rooms or {room_of(b) for b in boxes_in(ROOM_TOKEN.sub(" ", piece))}


def listed_rooms(piece: str, rest: list[str]) -> set[int]:
    if not ROOM_WORD.search(piece):
        return set()
    numbers = []
    for part in rest:
        if not BARE_NUMBER.fullmatch(part.strip()):
            break
        numbers.append(int(part))
    return set(numbers) if numbers and all(1 <= n <= ROOMS for n in numbers) else set()


def empty_rooms(text: str) -> set[int]:
    low = ROOM_LIST.sub(room_token, norm(text))
    out: set[int] = set()
    for clause in CLAUSE_END.split(low):
        pieces = PIECE_SPLIT.split(clause)
        assert len(pieces) <= MAX_TOKENS
        for i, piece in enumerate(pieces):
            if not EMPTY.search(piece) or HEDGE.search(piece):
                continue
            rooms = piece_rooms(piece) or listed_rooms(piece, pieces[i + 1 :])
            for near in (i - 1, i - 2, i + 1):
                if rooms:
                    break
                if 0 <= near < len(pieces) and not HEDGE.search(pieces[near]):
                    rooms = piece_rooms(pieces[near])
            out |= rooms
    return out


def claim_head(clause: str, seat: str) -> str:
    verb = CLAIM_VERB.search(clause)
    if verb is None:
        return ""
    before = list(CLAIM_CUT.finditer(clause, 0, verb.start()))
    if before and NEG_CUT.fullmatch(before[-1].group()):
        return ""
    stop = CLAIM_CUT.search(clause, verb.end())
    head = clause[before[-1].end() if before else 0 : stop.start() if stop else len(clause)]
    if not set(SEAT.findall(SEAT_ATTRIBUTION.sub(" ", head))) <= {seat[1:]}:
        return ""
    return head


def claim_boxes(low: str, seat: str) -> set[int]:
    out: set[int] = set()
    for clause in CLAUSE_END.split(PURPOSE.sub(" ", low)):
        out |= boxes_in(claim_head(clause, seat))
    return out


def seat_segments(low: str, seats: list[str]) -> dict[str, list[tuple[str, str]]]:
    hits = [m for m in SEAT.finditer(low) if f"p{m.group(1)}" in seats]
    assert len(hits) <= MAX_TOKENS
    ends = []
    for i, m in enumerate(hits):
        stop = hits[i + 1].start() if i + 1 < len(hits) else len(low)
        cut = CLAUSE_END.search(low, m.end(), stop)
        ends.append(cut.start() if cut else stop)
    texts = [low[m.end() : end] for m, end in zip(hits, ends, strict=True)]
    for i in range(len(hits) - 2, -1, -1):
        glued = GROUP_GLUE.fullmatch(texts[i]) and ends[i] == hits[i + 1].start()
        texts[i] = texts[i + 1] if glued else texts[i]
    out: dict[str, list[tuple[str, str]]] = {}
    for m, text in zip(hits, texts, strict=True):
        prefix = low[max(0, m.start() - 8) : m.start()]
        out.setdefault(f"p{m.group(1)}", []).append((prefix, text))
    return out


def assigning(prefix: str, part: str) -> bool:
    if not part.strip() or part.lstrip().startswith(")") or NOT_ASSIGN.search(part):
        return False
    if ATTRIBUTION.search(prefix):
        return False
    return not prefix.rstrip().endswith("(") or bool(ASSIGN_VERB.search(part))


def plan_of(text: str, seats: list[str]) -> dict | None:
    low = norm(text)
    if FORMULA.search(low):
        return {"formula": True, "targets": {}, "segments": {}, "named": list(seats)}
    segments = seat_segments(low, seats)
    targets: dict[str, frozenset[int]] = {}
    for seat, parts in segments.items():
        boxes: set[int] = set()
        for prefix, part in parts:
            if not assigning(prefix, part):
                continue
            found = boxes_in(TARGET_LEAD.sub(" ", part))
            boxes |= found if found else room_boxes(rooms_in(part))
        if boxes:
            targets[seat] = frozenset(boxes)
    if len(targets) < 2:
        return None
    joined = {seat: " | ".join(t for _, t in parts) for seat, parts in segments.items()}
    return {"formula": False, "targets": targets, "segments": joined, "named": sorted(targets)}


def first_time(trial: Path) -> float:
    with (trial / "verifier" / "events.jsonl").open() as handle:
        first = json.loads(handle.readline())
    assert first["seq"] == 1, trial
    return float(first["t"])


def treasure_rooms(task_dir: Path | None, instance: str, seen: set[int]) -> frozenset[int]:
    path = task_dir / instance / "environment" / "world" / "instance.json" if task_dir else None
    if path is None or not path.is_file():
        return frozenset(seen)
    rooms = frozenset(json.loads(path.read_text())["treasure_rooms"])
    assert len(rooms) == 2 and seen <= rooms, (path, rooms, seen)
    return rooms


def final_text(trial: Path, seat: str) -> str:
    path = trial / "seats" / seat / "agent" / "trajectory.json"
    if not path.is_file():
        return ""
    steps = json.loads(path.read_text()).get("steps") or []
    assert len(steps) <= MAX_STEPS, path
    texts = [
        str(s.get("message") or "").strip()
        for s in steps
        if isinstance(s, dict) and s.get("source") == "agent"
    ]
    if not texts:
        return ""
    last = texts[-1]
    if len(last) < SHORT_FINAL_TEXT and len(texts) > 1:
        last = texts[-2] + " " + last
    return re.sub(r"^analysis:\s*", "", last, flags=re.I)


def seat_meta(trial: Path, result: dict, seats: list[str], cap: int) -> dict[str, dict]:
    items = {item["seat"]: item for item in result["seat_results"]}
    out = {}
    for seat in seats:
        exc = items[seat].get("exception_info")
        kind = (exc.get("exception_type") or "error") if isinstance(exc, dict) else exc
        steps = agent_steps(trial, seat)
        out[seat] = {
            "error": None if exc is None else str(kind),
            "steps": steps,
            "cap": steps is not None and steps >= cap,
            "final": final_text(trial, seat),
        }
    return out


def note_post(tr: dict, e: dict, base: dict) -> None:
    text = e["response"]["text"]
    assert e["response"]["id"] == len(tr["posts"]) + 1, (tr["trial"], e["seq"])
    assert len(tr["posts"]) < MAX_MESSAGES
    low = norm(text)
    tr["posts"].append(
        base
        | {
            "id": e["response"]["id"],
            "text": text,
            "boxes": frozenset(boxes_in(low)),
            "empty": frozenset(empty_rooms(text)),
            "claim": frozenset(claim_boxes(low, base["seat"])),
            "plan": plan_of(text, tr["seats"]),
        }
    )


def note_event(tr: dict, e: dict, idx: int) -> None:
    seat, kind, method = e["seat"], e["kind"], e["method"]
    assert seat in tr["seats"], (tr["trial"], e["seq"])
    tr["last_seq"][seat] = e["seq"]
    base = {"seq": e["seq"], "t": e["t"] - tr["t0"], "idx": idx, "seat": seat}
    if (kind, method) == ("msg", "POST"):
        note_post(tr, e, base)
    elif (kind, method) == ("msgs", "GET"):
        ids = [m["id"] for m in e["response"]["messages"]]
        assert ids == list(range(1, len(ids) + 1)), (tr["trial"], e["seq"])
        reads = tr["reads"][seat]
        assert not reads or reads[-1][1] <= len(ids), (tr["trial"], e["seq"])
        reads.append((e["seq"], len(ids), idx, base["t"]))
    elif (kind, method) == ("open", "POST"):
        box = e["request"]["box"]
        assert e["response"]["box"] == box and e["response"]["room"] == room_of(box), e["seq"]
        tr["opens"].append(base | {"box": box, "room": room_of(box), "flag": e["response"]["flag"]})


def load_trial(trial: Path, arm: dict, cfg: dict) -> dict | None:
    result = json.loads((trial / "result.json").read_text())
    seats = sorted(item["seat"] for item in result["seat_results"])
    assert len(seats) == arm["seats"], (trial, seats)
    reward = read_reward(trial, result)
    if reward is None:
        return None
    tr = {
        "trial": trial.name,
        "instance": trial.name.split("__", 1)[0],
        "seats": seats,
        "k": len(seats),
        "budget": arm["budget"],
        "reward": reward,
        "t0": first_time(trial),
        "posts": [],
        "opens": [],
        "reads": {s: [] for s in seats},
        "last_seq": {},
    }
    for idx, e in enumerate(played(trial), start=1):
        note_event(tr, e, idx)
    flag_boxes = {o["box"] for o in tr["opens"] if o["flag"]}
    assert abs(reward - len(flag_boxes) / cfg["flags"]) < 1e-6, trial
    task_dir = cfg["tasks"] / arm["label"] if cfg["tasks"] else None
    tr["treasure"] = treasure_rooms(task_dir, tr["instance"], {room_of(b) for b in flag_boxes})
    assert all(o["flag"] == (o["room"] in tr["treasure"]) for o in tr["opens"]), trial
    tr["meta"] = seat_meta(trial, result, seats, cfg["cap"])
    tr["plans"] = [p for p in tr["posts"] if p["plan"]]
    return tr


def read_top(tr: dict, seat: str, seq: int) -> int:
    reads = tr["reads"][seat]
    i = bisect.bisect_left(reads, (seq,))
    return reads[i - 1][1] if i else 0


def knows(tr: dict, seat: str, post: dict, seq: int) -> bool:
    return post["seq"] < seq and (post["seat"] == seat or post["id"] <= read_top(tr, seat, seq))


def seat_timeline(tr: dict, seat: str) -> dict:
    reads = tr["reads"][seat]
    posts = [p for p in tr["posts"] if p["seat"] == seat]
    opens = [o for o in tr["opens"] if o["seat"] == seat]
    full = [r for r in reads if r[1] > 0]
    firsts = {
        "R": (reads[0][0], reads[0][2], reads[0][3]) if reads else None,
        "P": (posts[0]["seq"], posts[0]["idx"], posts[0]["t"]) if posts else None,
        "O": (opens[0]["seq"], opens[0]["idx"], opens[0]["t"]) if opens else None,
    }
    order = "".join(k for k, v in sorted(firsts.items(), key=lambda kv: kv[1] or ()) if v)
    first_open = opens[0]["seq"] if opens else None
    first_msg = tr["posts"][0]["seq"] if tr["posts"] else None
    out = {"seat": seat, "order": order, "opened": first_open is not None}
    for key, name in (("R", "read"), ("P", "post"), ("O", "open")):
        out[name + "_idx"] = firsts[key][1] if firsts[key] else None
        out[name + "_t"] = firsts[key][2] if firsts[key] else None
    if first_open is not None:
        out["before_any_msg"] = first_msg is None or first_open < first_msg
        out["before_read_msg"] = not any(r[0] < first_open for r in full)
        out["before_read_call"] = not any(r[0] < first_open for r in reads)
        out["posted_before"] = any(p["seq"] < first_open for p in posts)
    return out


def result_posts(tr: dict) -> list[dict]:
    out = []
    for p in tr["posts"]:
        mine = {o["box"] for o in tr["opens"] if o["seat"] == p["seat"] and o["seq"] < p["seq"]}
        if mine & p["boxes"]:
            out.append(p)
    return out


def first_round(tr: dict) -> dict:
    reports = result_posts(tr)
    end = None
    for seat in tr["seats"]:
        for seq, top, _, t in tr["reads"][seat]:
            if any(p["seat"] != seat and p["id"] <= top for p in reports):
                end = (seq, t) if end is None or seq < end[0] else end
                break
    opens = [o for o in tr["opens"] if end is None or o["seq"] < end[0]]
    return {
        "end_seq": end[0] if end else None,
        "end_t": end[1] if end else None,
        "opens": len(opens),
        "repeats": len(opens) - len({o["box"] for o in opens}),
    }


def governing(tr: dict, seat: str, seq: int) -> dict | None:
    for p in reversed(tr["plans"]):
        plan = p["plan"]
        if not plan["formula"] and seat in plan["targets"] and knows(tr, seat, p, seq):
            return p
    return None


def competing(plans: list[dict]) -> int:
    count = 0
    for j, p in enumerate(plans):
        mine = p["plan"]["targets"]
        for q in plans[:j]:
            theirs = q["plan"]["targets"]
            shared = set(mine) & set(theirs)
            if q["seat"] != p["seat"] and any(not mine[s] & theirs[s] for s in shared):
                count += 1
                break
    return count


def tail_status(plan: dict, seat: str) -> str:
    if plan["formula"]:
        return "formula"
    targets = plan["targets"]
    if seat in targets:
        rooms = {room_of(b) for b in targets[seat]}
        others = {room_of(b) for s, bs in targets.items() if s != seat for b in bs}
        return "assigned, shared room" if rooms & others else "assigned, own room"
    segment = plan["segments"].get(seat)
    if segment is None:
        return "omitted"
    return "reserve" if RESERVE.search(segment) else "mentioned"


def plan_stats(tr: dict) -> dict:
    plans = tr["plans"]
    out: dict = {"n": len(plans), "authors": len({p["seat"] for p in plans}), "obey": Counter()}
    out["formula"] = sum(p["plan"]["formula"] for p in plans)
    out["competing"] = competing([p for p in plans if not p["plan"]["formula"]])
    out["named"] = [len(p["plan"]["named"]) for p in plans]
    out["tail"] = Counter()
    for p in plans:
        if len(p["plan"]["named"]) < ROOMS:
            continue
        out["tail_plans"] = out.get("tail_plans", 0) + 1
        for seat in tr["seats"][ROOMS:]:
            out["tail"][tail_status(p["plan"], seat)] += 1
    if not plans:
        return out
    first = plans[0]
    out["first"] = {
        "seq": first["seq"],
        "t": first["t"],
        "seat": first["seat"],
        "opens_before": sum(o["seq"] < first["seq"] for o in tr["opens"]),
    }
    done: set[str] = set()
    for o in tr["opens"]:
        if o["seq"] < first["seq"]:
            continue
        g = governing(tr, o["seat"], o["seq"])
        call = (
            "free"
            if g is None
            else ("obey" if o["box"] in g["plan"]["targets"][o["seat"]] else "defy")
        )
        out["obey"][call] += 1
        if g is not None and o["seat"] not in done:
            done.add(o["seat"])
            out["obey"]["first_" + call] += 1
    return out


def reports_of(tr: dict, earlier: list[dict], seq: int) -> list[dict]:
    box = earlier[0]["box"]
    opened = {}
    for x in earlier:
        opened.setdefault(x["seat"], x["seq"])
    return [
        p
        for p in tr["posts"]
        if p["seq"] < seq and box in p["boxes"] and opened.get(p["seat"], seq) < p["seq"]
    ]


def repeat_kind(tr: dict, earlier: list[dict], o: dict) -> tuple[str, str]:
    first, b, box = earlier[0], o["seat"], o["box"]
    a = first["seat"]
    if any(x["seat"] == b for x in earlier):
        return "own", ""
    g = governing(tr, b, o["seq"])
    if g is not None and box in g["plan"]["targets"][b]:
        h = governing(tr, a, first["seq"])
        return "assign", "both_assigned" if h and box in h["plan"]["targets"][a] else ""
    a_posts = [p for p in tr["posts"] if p["seat"] == a and p["seq"] < o["seq"]]
    b_posts = [p for p in tr["posts"] if p["seat"] == b and p["seq"] < first["seq"]]
    b_saw_a = any(knows(tr, b, p, o["seq"]) for p in a_posts)
    a_saw_b = any(knows(tr, a, p, first["seq"]) for p in b_posts)
    if not b_saw_a and not a_saw_b:
        return "simul", ""
    reports = reports_of(tr, earlier, o["seq"])
    if not reports:
        announced = any(box in p["boxes"] and knows(tr, b, p, o["seq"]) for p in a_posts)
        return "unreported", "announced_read" if announced else ""
    if not any(knows(tr, b, p, o["seq"]) for p in reports):
        return "unread", ""
    return "read", ""


def lowest_free(tr: dict, o: dict) -> bool:
    known = {x["box"] for x in tr["opens"] if x["seat"] == o["seat"] and x["seq"] < o["seq"]}
    for p in tr["posts"]:
        if knows(tr, o["seat"], p, o["seq"]):
            done = {x["box"] for x in tr["opens"] if x["seat"] == p["seat"] and x["seq"] < p["seq"]}
            known |= p["boxes"] & done
    free = room_boxes({o["room"]}) - known
    return bool(free) and o["box"] == min(free)


def repeats(tr: dict) -> list[dict]:
    seen: dict[int, list[dict]] = {}
    seat_first: dict[str, int] = {}
    found: set[int] = set()
    out = []
    for o in tr["opens"]:
        earlier = seen.setdefault(o["box"], [])
        seat_first.setdefault(o["seat"], o["seq"])
        if earlier:
            kind, sub = repeat_kind(tr, earlier, o)
            mentioned = [p for p in tr["posts"] if o["box"] in p["boxes"]]
            out.append(
                {
                    "seq": o["seq"],
                    "seat": o["seat"],
                    "box": o["box"],
                    "first_seat": earlier[0]["seat"],
                    "first_seq": earlier[0]["seq"],
                    "kind": kind,
                    "sub": sub,
                    "informed_any": any(knows(tr, o["seat"], p, o["seq"]) for p in mentioned),
                    "room_first": o["box"] in ROOM_FIRST,
                    "seat_first_open": o["seq"] == seat_first[o["seat"]],
                    "lowest_free": lowest_free(tr, o),
                    "gap_t": o["t"] - earlier[0]["t"],
                    "flag": o["flag"],
                    "rooms_found": len(found),
                }
            )
        earlier.append(o)
        found |= {o["room"]} if o["flag"] else set()
    return out


def phase_shares(reps: list[dict]) -> dict:
    found = Counter(x["rooms_found"] for x in reps)
    return {f"found{n}": share(found[n], len(reps)) for n in range(3)}


def claim_stats(tr: dict) -> dict:
    claimants: dict[int, list[str]] = {}
    posts = claims(tr, None)
    for p in posts:
        for box in sorted(p["claim"]):
            names = claimants.setdefault(box, [])
            names.extend([] if p["seat"] in names else [p["seat"]])
    opened: dict[int, set[str]] = {}
    for o in tr["opens"]:
        opened.setdefault(o["box"], set()).add(o["seat"])
    multi = {b: names for b, names in claimants.items() if len(names) > 1}
    return {
        "claim_posts": len(posts),
        "pairs": sum(len(names) for names in claimants.values()),
        "boxes": len(claimants),
        "multi": len(multi),
        "multi_unopened": sum(b not in opened for b in multi),
        "first_yields": sum(names[0] not in opened.get(b, set()) for b, names in multi.items()),
        "pairs_opened": sum(
            len(set(names) & opened.get(b, set())) for b, names in claimants.items()
        ),
    }


def report_lag(tr: dict) -> dict:
    firsts: dict[int, dict] = {}
    for o in tr["opens"]:
        firsts.setdefault(o["box"], o)
    out: dict = {"new": len(firsts), "never": 0, "lags": [], "others": [], "own": []}
    for o in firsts.values():
        report = next(
            (
                p
                for p in tr["posts"]
                if p["seat"] == o["seat"] and p["seq"] > o["seq"] and o["box"] in p["boxes"]
            ),
            None,
        )
        stop = report["seq"] if report else None
        window = [
            x for x in tr["opens"] if x["seq"] > o["seq"] and (stop is None or x["seq"] < stop)
        ]
        out["never"] += report is None
        if report is not None:
            out["lags"].append(report["t"] - o["t"])
        out["others"].append(sum(x["seat"] != o["seat"] for x in window))
        out["own"].append(sum(x["seat"] == o["seat"] for x in window))
    out["lowest_free_opens"] = sum(lowest_free(tr, o) for o in tr["opens"])
    return out


def scouting(tr: dict) -> Counter:
    c: Counter = Counter()
    empty_posts = [p for p in tr["posts"] if p["empty"]]
    c["wrong_empty_posts"] = sum(bool(p["empty"] & tr["treasure"]) for p in empty_posts)
    for o in tr["opens"]:
        if o["flag"]:
            continue
        prior = [x for x in tr["opens"] if x["room"] == o["room"] and x["seq"] < o["seq"]]
        said = [p for p in empty_posts if o["room"] in p["empty"] and p["seq"] < o["seq"]]
        c["empty_opens"] += 1
        c["first_probe"] += not prior
        c["after_any_open"] += bool(prior)
        c["after_own_open"] += any(x["seat"] == o["seat"] for x in prior)
        c["after_msg"] += bool(said)
        c["after_read"] += any(knows(tr, o["seat"], p, o["seq"]) for p in said)
        c["repeat_box"] += any(x["box"] == o["box"] for x in prior)
    c["rooms_probed"] = len({o["room"] for o in tr["opens"] if not o["flag"]})
    return c


def verdict(name: str, free: int) -> str:
    assert free >= 0
    if name != "nothing useful":
        return name
    return "nothing useful, " + ("true" if free == 0 else "false")


def reason(meta: dict) -> str:
    if meta["error"]:
        return "error/time"
    if meta["cap"]:
        return "turn cap"
    text = norm(meta["final"])
    for name, pattern in (
        ("claim text", CLAIM_TEXT),
        ("nothing useful", USELESS_TEXT),
        ("waiting", WAIT_TEXT),
    ):
        if pattern.search(text):
            return name
    return "other"


def is_claim(tr: dict, p: dict) -> bool:
    return 0 < len(p["claim"]) <= tr["budget"]


def claims(tr: dict, seat: str | None) -> list[dict]:
    return [p for p in tr["posts"] if is_claim(tr, p) and seat in (None, p["seat"])]


def lost_claim(tr: dict, seat: str) -> bool:
    opened = {o["box"] for o in tr["opens"] if o["seat"] == seat}
    rivals = [q for q in claims(tr, None) if q["seat"] != seat]
    for p in claims(tr, seat):
        for box in p["claim"] - opened:
            if any(q["seq"] < p["seq"] and box in q["claim"] for q in rivals):
                return True
    return False


def stranded(tr: dict) -> list[dict]:
    flags = room_boxes(set(tr["treasure"]))
    out = []
    for seat in tr["seats"]:
        mine = [o for o in tr["opens"] if o["seat"] == seat]
        if len(mine) >= tr["budget"]:
            continue
        last = tr["last_seq"].get(seat, 0)
        opened = {o["box"] for o in tr["opens"] if o["seq"] <= last}
        mentioned = set().union(*(p["boxes"] for p in tr["posts"] if p["seq"] <= last))
        free = flags - opened
        meta = tr["meta"][seat]
        own = [p for p in tr["posts"] if p["seat"] == seat]
        out.append(
            {
                "seat": seat,
                "unused": tr["budget"] - len(mine),
                "never": not mine,
                "reason": verdict(reason(meta), len(free)),
                "lost_claim": lost_claim(tr, seat),
                "free_flags": len(free),
                "free_unmentioned": len(free - mentioned),
                "steps": meta["steps"],
                "final": meta["final"],
                "last_post": [own[-1]["seq"], own[-1]["text"]] if own else None,
            }
        )
    return out


def ending(tr: dict) -> dict:
    flags = room_boxes(set(tr["treasure"]))
    missing = flags - {o["box"] for o in tr["opens"]}
    mentioned = set().union(*(p["boxes"] for p in tr["posts"]))
    used = Counter(o["seat"] for o in tr["opens"])
    claimant: dict[int, str] = {}
    for p in claims(tr, None):
        for box in p["claim"]:
            claimant.setdefault(box, p["seat"])
    held = [claimant[b] for b in missing if b in claimant]
    return {
        "missing": len(missing),
        "missing_mentioned": len(missing & mentioned),
        "missing_claimed": len(held),
        "missing_claimed_spent": sum(used[s] >= tr["budget"] for s in held),
        "missing_claimed_left": sum(used[s] < tr["budget"] for s in held),
        "unused": tr["k"] * tr["budget"] - len(tr["opens"]),
        "rooms_found": len({o["room"] for o in tr["opens"] if o["flag"]}),
    }


def analyze_trial(tr: dict) -> dict:
    plans = plan_stats(tr)
    if tr["plans"]:
        plans["first"]["text"] = tr["plans"][0]["text"]
    flags = {o["box"] for o in tr["opens"] if o["flag"]}
    reps = repeats(tr)
    return {
        "trial": tr["trial"],
        "reward": tr["reward"],
        "k": tr["k"],
        "seats": [seat_timeline(tr, s) for s in tr["seats"]],
        "round": first_round(tr),
        "plans": plans,
        "repeats": reps,
        "lag": report_lag(tr),
        "claims": claim_stats(tr),
        "scouting": scouting(tr),
        "stranded": stranded(tr),
        "end": ending(tr),
        "posts": len(tr["posts"]),
        "reads": sum(len(r) for r in tr["reads"].values()),
        "opens": len(tr["opens"]),
        "flags": len(flags),
        "empty": len({o["box"] for o in tr["opens"] if not o["flag"]}),
        "n_repeats": len(reps),
        "total": tr["k"] * tr["budget"],
    }


def mean_or_none(values: list) -> float | None:
    kept = [v for v in values if v is not None]
    return fmean(kept) if kept else None


def share(part: int | float, whole: int | float) -> float | None:
    return part / whole if whole else None


def q1_row(rows: list[dict]) -> dict:
    seats = [s for r in rows for s in r["seats"]]
    opened = [s for s in seats if s["opened"]]
    orders = Counter(s["order"] for s in seats)
    ends = [r["round"]["end_t"] for r in rows]
    return {
        "seats": len(seats),
        "read_t": mean_or_none([s["read_t"] for s in seats]),
        "post_t": mean_or_none([s["post_t"] for s in seats]),
        "open_t": mean_or_none([s["open_t"] for s in seats]),
        "open_idx_median": median([s["open_idx"] for s in opened]) if opened else None,
        "before_any_msg": share(sum(s["before_any_msg"] for s in opened), len(opened)),
        "before_read_msg": share(sum(s["before_read_msg"] for s in opened), len(opened)),
        "before_read_call": share(sum(s["before_read_call"] for s in opened), len(opened)),
        "posted_before": share(sum(s["posted_before"] for s in opened), len(opened)),
        "orders": {k: v / len(seats) for k, v in orders.most_common(4)},
        "round1_opens": fmean(r["round"]["opens"] for r in rows),
        "round1_share": fmean(r["round"]["opens"] / r["total"] for r in rows),
        "round1_repeats": fmean(r["round"]["repeats"] for r in rows),
        "round1_end_t": mean_or_none(ends),
        "no_round_end": sum(e is None for e in ends),
    }


def q2_row(rows: list[dict], k: int) -> dict:
    with_plan = [r for r in rows if r["plans"]["n"]]
    obey: Counter = sum((r["plans"]["obey"] for r in rows), Counter())
    named = [n for r in rows for n in r["plans"]["named"]]
    tail: Counter = sum((r["plans"]["tail"] for r in rows), Counter())
    authors = Counter(r["plans"]["first"]["seat"] for r in with_plan)
    judged = obey["obey"] + obey["defy"]
    first_judged = obey["first_obey"] + obey["first_defy"]
    return {
        "trials": len(rows),
        "plan_share": len(with_plan) / len(rows),
        "plans": fmean(r["plans"]["n"] for r in rows),
        "authors": fmean(r["plans"]["authors"] for r in rows),
        "competing": fmean(r["plans"]["competing"] for r in rows),
        "formula": sum(r["plans"]["formula"] for r in rows),
        "first_opens_before": mean_or_none(
            [r["plans"]["first"]["opens_before"] for r in with_plan]
        ),
        "first_t": mean_or_none([r["plans"]["first"]["t"] for r in with_plan]),
        "first_authors": {s: v / len(with_plan) for s, v in sorted(authors.items())},
        "named": mean_or_none(named),
        "named_all": share(sum(n == k for n in named), len(named)),
        "after_opens": sum(obey[key] for key in ("obey", "defy", "free")),
        "free_share": share(obey["free"], sum(obey[key] for key in ("obey", "defy", "free"))),
        "obey_share": share(obey["obey"], judged),
        "first_obey_share": share(obey["first_obey"], first_judged),
        "tail_plans": sum(r["plans"].get("tail_plans", 0) for r in rows),
        "tail": dict(sorted(tail.items())),
    }


def q3_row(rows: list[dict]) -> dict:
    reps = [x for r in rows for x in r["repeats"]]
    kinds = Counter(x["kind"] for x in reps)
    subs = Counter(x["sub"] for x in reps)
    out = {"repeats": len(reps), "per_trial": len(reps) / len(rows)}
    out |= {kind: share(kinds[kind], len(reps)) for kind in REPEAT_KINDS}
    out |= {
        "announced_read": subs["announced_read"],
        "both_assigned": subs["both_assigned"],
        "informed_any": share(sum(x["informed_any"] for x in reps), len(reps)),
        "room_first": share(sum(x["room_first"] for x in reps), len(reps)),
        "flag_box": share(sum(x["flag"] for x in reps), len(reps)),
        "seat_first_open": share(sum(x["seat_first_open"] for x in reps), len(reps)),
        "counts": dict(kinds),
        **phase_shares(reps),
    }
    return out


def q3b_row(rows: list[dict]) -> dict:
    lags = [x for r in rows for x in r["lag"]["lags"]]
    others = [x for r in rows for x in r["lag"]["others"]]
    own = [x for r in rows for x in r["lag"]["own"]]
    reps = [x for r in rows for x in r["repeats"]]
    new = sum(r["lag"]["new"] for r in rows)
    return {
        "new_boxes": new,
        "never_reported": share(sum(r["lag"]["never"] for r in rows), new),
        "lag_mean_s": mean_or_none(lags),
        "lag_median_s": median(lags) if lags else None,
        "other_opens_in_lag": mean_or_none(others),
        "own_opens_in_lag": mean_or_none(own),
        "repeat_gap_median_s": median([x["gap_t"] for x in reps]) if reps else None,
        "lowest_free_repeats": share(sum(x["lowest_free"] for x in reps), len(reps)),
        "lowest_free_opens": share(
            sum(r["lag"]["lowest_free_opens"] for r in rows), sum(r["opens"] for r in rows)
        ),
    }


def q4_row(rows: list[dict]) -> dict:
    c: Counter = sum((r["scouting"] for r in rows), Counter())
    total = c["empty_opens"]
    keys = (
        "first_probe",
        "after_any_open",
        "after_own_open",
        "after_msg",
        "after_read",
        "repeat_box",
    )
    out = {"empty_opens": total, "per_trial": total / len(rows)}
    out |= {key: c[key] for key in keys}
    out |= {key + "_share": share(c[key], total) for key in keys}
    out["rooms_probed"] = c["rooms_probed"] / len(rows)
    out["wrong_empty_posts"] = c["wrong_empty_posts"]
    return out


def q5_row(rows: list[dict]) -> dict:
    seats = [s for r in rows for s in r["stranded"]]
    opens = sum(r["opens"] for r in rows)
    k = rows[0]["k"]
    return {
        "stranded_seats": len(seats),
        "unused": sum(s["unused"] for s in seats),
        "unused_per_trial": sum(s["unused"] for s in seats) / len(rows),
        "never_opened": sum(s["never"] for s in seats),
        "reasons": {name: sum(s["reason"] == name for s in seats) for name in REASONS},
        "lost_claim": sum(s["lost_claim"] for s in seats),
        "free_flags_at_stop": mean_or_none([s["free_flags"] for s in seats]),
        "free_unmentioned_at_stop": mean_or_none([s["free_unmentioned"] for s in seats]),
        "stop_with_unmentioned": sum(s["free_unmentioned"] > 0 for s in seats),
        "missing": fmean(r["end"]["missing"] for r in rows),
        "missing_mentioned": fmean(r["end"]["missing_mentioned"] for r in rows),
        "missing_claimed": fmean(r["end"]["missing_claimed"] for r in rows),
        "missing_claimed_spent": fmean(r["end"]["missing_claimed_spent"] for r in rows),
        "missing_claimed_left": fmean(r["end"]["missing_claimed_left"] for r in rows),
        "posts_per_open": share(sum(r["posts"] for r in rows), opens),
        "reads_per_open": share(sum(r["reads"] for r in rows), opens),
        "posts_per_seat": sum(r["posts"] for r in rows) / (len(rows) * k),
        "reads_per_seat": sum(r["reads"] for r in rows) / (len(rows) * k),
    }


def q5c_row(rows: list[dict]) -> dict:
    c: Counter = sum((Counter(r["claims"]) for r in rows), Counter())
    n = len(rows)
    return {
        "claim_posts": c["claim_posts"] / n,
        "claimed_boxes": c["boxes"] / n,
        "claimants_per_box": share(c["pairs"], c["boxes"]),
        "multi_claimed": c["multi"] / n,
        "multi_never_opened": c["multi_unopened"] / n,
        "first_claimant_yields": c["first_yields"] / n,
        "claims_kept": share(c["pairs_opened"], c["pairs"]),
    }


def q6_row(rows: list[dict]) -> dict:
    k = rows[0]["k"]
    reps = [x for r in rows for x in r["repeats"]]
    return {
        "k": k,
        "reward": fmean(r["reward"] for r in rows),
        "flags": fmean(r["flags"] for r in rows),
        "empty": fmean(r["empty"] for r in rows),
        "repeats": len(reps) / len(rows),
        "unused": fmean(r["end"]["unused"] for r in rows),
        "round1_repeats": fmean(r["round"]["repeats"] for r in rows),
        "later_repeats": len(reps) / len(rows) - fmean(r["round"]["repeats"] for r in rows),
        "plans": fmean(r["plans"]["n"] for r in rows),
        "competing": fmean(r["plans"]["competing"] for r in rows),
        "posts_per_seat": sum(r["posts"] for r in rows) / (len(rows) * k),
        "reads_per_seat": sum(r["reads"] for r in rows) / (len(rows) * k),
        "rooms_found": fmean(r["end"]["rooms_found"] for r in rows),
    }


def q7_rows(rows: list[dict]) -> list[dict]:
    ranked = sorted(rows, key=lambda r: (-r["reward"], r["n_repeats"], r["trial"]))
    picked = ranked[:2] + ranked[-2:] if len(ranked) > 4 else ranked
    out = []
    for r in picked:
        first = r["plans"].get("first") or {}
        out.append(
            {
                "trial": r["trial"],
                "reward": r["reward"],
                "flags": r["flags"],
                "empty": r["empty"],
                "repeats": r["n_repeats"],
                "unused": r["end"]["unused"],
                "round1_opens": r["round"]["opens"],
                "plans": r["plans"]["n"],
                "competing": r["plans"]["competing"],
                "first_plan_after": first.get("opens_before"),
                "first_plan_seat": first.get("seat"),
                "posts": r["posts"],
                "rooms_found": r["end"]["rooms_found"],
                "kinds": dict(Counter(x["kind"] for x in r["repeats"])),
            }
        )
    return out


def shares_text(values: dict) -> str:
    return " ".join(f"{k}:{cell(v)}" for k, v in values.items()) or "-"


def arm_rows(arms: list[dict], key: str, columns: tuple) -> list[list[str]]:
    rows = []
    for a in arms:
        row = a[key]
        rows.append(
            [a["label"], *(cell(row[c], d) if d >= 0 else shares_text(row[c]) for c, d in columns)]
        )
    return rows


Q_TABLES = (
    (
        "Q1a. First read, first post, first open per seat (seconds from the first event)",
        "q1",
        (
            ("seats", 0),
            ("read_t", 1),
            ("post_t", 1),
            ("open_t", 1),
            ("open_idx_median", 1),
            ("before_any_msg", 2),
            ("before_read_msg", 2),
            ("before_read_call", 2),
            ("posted_before", 2),
            ("orders", -1),
        ),
    ),
    (
        "Q1b. First round: opens before any seat has read another seat's result report",
        "q1",
        (
            ("round1_opens", 1),
            ("round1_share", 2),
            ("round1_repeats", 2),
            ("round1_end_t", 1),
            ("no_round_end", 0),
        ),
    ),
    (
        "Q2a. Assignment messages (plans)",
        "q2",
        (
            ("trials", 0),
            ("plan_share", 2),
            ("plans", 2),
            ("authors", 2),
            ("competing", 2),
            ("formula", 0),
            ("first_opens_before", 2),
            ("first_t", 1),
            ("named", 2),
            ("named_all", 2),
        ),
    ),
    (
        "Q2b. Obedience after the first plan; first plan author; p7 p8 in plans naming 6+ seats",
        "q2",
        (
            ("after_opens", 0),
            ("free_share", 2),
            ("obey_share", 2),
            ("first_obey_share", 2),
            ("first_authors", -1),
            ("tail_plans", 0),
            ("tail", -1),
        ),
    ),
    (
        "Q3. Repeats by kind (shares of all repeats)",
        "q3",
        (
            ("repeats", 0),
            ("per_trial", 2),
            *((kind, 2) for kind in REPEAT_KINDS),
            ("announced_read", 0),
            ("both_assigned", 0),
            ("informed_any", 2),
            ("room_first", 2),
            ("seat_first_open", 2),
            ("flag_box", 2),
            ("found0", 2),
            ("found1", 2),
            ("found2", 2),
        ),
    ),
    (
        "Q3b. Report lag: from a new open to the opener's first post that names the box",
        "q3b",
        (
            ("new_boxes", 0),
            ("never_reported", 2),
            ("lag_mean_s", 1),
            ("lag_median_s", 1),
            ("other_opens_in_lag", 2),
            ("own_opens_in_lag", 2),
            ("repeat_gap_median_s", 1),
            ("lowest_free_repeats", 2),
            ("lowest_free_opens", 2),
        ),
    ),
    (
        "Q4. Opens in empty rooms",
        "q4",
        (
            ("empty_opens", 0),
            ("per_trial", 2),
            ("first_probe", 0),
            ("after_any_open", 0),
            ("after_own_open", 0),
            ("after_msg", 0),
            ("after_read", 0),
            ("repeat_box", 0),
            ("rooms_probed", 2),
            ("wrong_empty_posts", 0),
        ),
    ),
    (
        "Q5a. Seats that stop with opens left",
        "q5",
        (
            ("stranded_seats", 0),
            ("unused", 0),
            ("unused_per_trial", 2),
            ("never_opened", 0),
            ("reasons", -1),
            ("lost_claim", 0),
        ),
    ),
    (
        "Q5b. Flag boxes left at stop and at the end; message spend",
        "q5",
        (
            ("free_flags_at_stop", 2),
            ("free_unmentioned_at_stop", 2),
            ("stop_with_unmentioned", 0),
            ("missing", 2),
            ("missing_mentioned", 2),
            ("missing_claimed", 2),
            ("missing_claimed_spent", 2),
            ("missing_claimed_left", 2),
            ("posts_per_open", 2),
            ("reads_per_open", 2),
            ("posts_per_seat", 2),
            ("reads_per_seat", 2),
        ),
    ),
    (
        "Q5c. Claim traffic (claim = clause with a claim verb, not negated, one seat, 1..budget boxes)",
        "q5c",
        (
            ("claim_posts", 2),
            ("claimed_boxes", 2),
            ("claimants_per_box", 2),
            ("multi_claimed", 2),
            ("multi_never_opened", 2),
            ("first_claimant_yields", 2),
            ("claims_kept", 2),
        ),
    ),
    (
        "Q6. Per-trial means by k",
        "q6",
        (
            ("k", 0),
            ("reward", 3),
            ("flags", 2),
            ("empty", 2),
            ("repeats", 2),
            ("unused", 2),
            ("round1_repeats", 2),
            ("later_repeats", 2),
            ("plans", 2),
            ("competing", 2),
            ("posts_per_seat", 2),
            ("reads_per_seat", 2),
            ("rooms_found", 2),
        ),
    ),
)


def render(report: dict) -> str:
    arms = report["arms"]
    parts = [
        "Trials: " + ", ".join(f"{a['label']}={a['trials']} (skipped {a['skipped']})" for a in arms)
    ]
    for title, key, columns in Q_TABLES:
        headers = ["arm", *(c for c, _ in columns)]
        parts.append(table(title, headers, arm_rows(arms, key, columns)))
    for label, rows in report["q7"].items():
        keys = list(rows[0]) if rows else []
        body = [[cell(r[c]) if c != "kinds" else shares_text(r[c]) for c in keys] for r in rows]
        parts.append(table(f"Q7. {label}: 2 best and 2 worst trials", keys, body))
    return "\n\n".join(parts)


def audit(loaded: dict[str, list[dict]], n: int) -> str:
    rng = random.Random(AUDIT_SEED)
    posts = [(label, tr, p) for label, trs in loaded.items() for tr in trs for p in tr["posts"]]
    multi = [x for x in posts if len(set(SEAT.findall(norm(x[2]["text"])))) >= 2]
    pools = (
        ("plan", [x for x in multi if x[2]["plan"]]),
        ("not plan, 2+ seats named", [x for x in multi if not x[2]["plan"]]),
        ("empty room said", [x for x in posts if x[2]["empty"]]),
        (
            "empty word, no room found",
            [x for x in posts if not x[2]["empty"] and EMPTY.search(norm(x[2]["text"]))],
        ),
        ("claim", [x for x in posts if is_claim(x[1], x[2])]),
        (
            "boxes named, not a claim",
            [x for x in posts if x[2]["boxes"] and not is_claim(x[1], x[2])],
        ),
    )
    lines = []
    for name, pool in pools:
        lines.append(f"== {name}: {len(pool)} posts, sample {min(n, len(pool))}")
        for label, tr, p in rng.sample(pool, min(n, len(pool))):
            plan = p["plan"]
            targets = {s: sorted(v) for s, v in plan["targets"].items()} if plan else None
            lines.append(f"{label} {tr['trial']} seq {p['seq']} {p['seat']}: {p['text']}")
            lines.append(f"   plan={targets} formula={bool(plan and plan['formula'])}")
            lines.append(f"   empty={sorted(p['empty'])} boxes={sorted(p['boxes'])}")
            lines.append(f"   claim={sorted(p['claim'])}")
    return "\n".join(lines)


def tasks_root(arm: dict, given: str | None) -> Path | None:
    root = Path(given) if given else arm["job"].parent.parent / "tasks" / "crowd"
    path = root / arm["label"]
    return root if path.is_dir() else None


def build(arms: list[dict], args: argparse.Namespace) -> tuple[dict, dict]:
    loaded: dict[str, list[dict]] = {}
    out = []
    q7 = {}
    traces = {}
    for arm in arms:
        cfg = {"flags": args.flags, "cap": args.turn_cap, "tasks": tasks_root(arm, args.tasks)}
        found = trial_dirs(arm["job"])
        trs = [tr for tr in (load_trial(t, arm, cfg) for t in found) if tr is not None]
        assert trs, f"{arm['label']}: no scored trials"
        loaded[arm["label"]] = trs
        rows = [analyze_trial(tr) for tr in trs]
        traces[arm["label"]] = rows
        k = arm["seats"]
        out.append(
            {
                "label": arm["label"],
                "trials": len(rows),
                "skipped": len(found) - len(rows),
                "q1": q1_row(rows),
                "q2": q2_row(rows, k),
                "q3": q3_row(rows),
                "q3b": q3b_row(rows),
                "q4": q4_row(rows),
                "q5": q5_row(rows),
                "q5c": q5c_row(rows),
                "q6": q6_row(rows),
            }
        )
        if k == 8 and arm["kind"] in CHANNEL_KINDS:
            q7[arm["label"]] = q7_rows(rows)
    return {"arms": out, "q7": q7, "trials": traces}, loaded


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crowd_messages.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=JOB_DIR")
    parser.add_argument("--tasks", metavar="TASKS_DIR")
    parser.add_argument("--total-budget", type=int, default=24)
    parser.add_argument("--flags", type=int, default=20)
    parser.add_argument("--turn-cap", type=int, default=30)
    parser.add_argument("--audit", type=int, default=0, metavar="N")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    assert 0 < args.flags <= BOXES and args.turn_cap > 0 and 0 <= args.audit <= MAX_MESSAGES
    arms = [parse_arm(text, args.total_budget, None) for text in args.arm]
    assert len({a["label"] for a in arms}) == len(arms), "arm labels repeat"
    assert all(a["seats"] > 1 for a in arms), "crowd_messages wants multi-seat arms"
    report, loaded = build(arms, args)
    if args.audit:
        print(audit(loaded, args.audit))
        return 0
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
