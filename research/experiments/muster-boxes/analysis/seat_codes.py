import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean, median

ROOM_SIZE = 10
RANGE_SPAN_MAX = ROOM_SIZE - 2
SLOW_AFTER_OPENS = 2
CHURN_CLAIMS = 3
CLAIM_BOXES_MAX = 3
MAX_ARMS = 64
MAX_TRIALS = 1000
MAX_EVENTS = 100_000
MAX_SEATS = 64
RATERS = (1, 2, 3, 4)
JUDGED = (
    "DUP_SEEN",
    "DUP_UNSEEN",
    "EMPTY_ROOM_OPEN",
    "EARLY_STOP",
    "SLOW_REPORT",
    "CLAIM_CHURN",
)
PLAIN = ("REPEAT_ANY", "REPEAT_FIRST", "REPEAT_LATER", "REPEAT_OWN", "OPENS_LEFT")
CODES = JUDGED + PLAIN
SHORT = {
    "DUP_SEEN": "dupSeen",
    "DUP_UNSEEN": "dupUnseen",
    "EMPTY_ROOM_OPEN": "emptyRm",
    "EARLY_STOP": "earlyStop",
    "SLOW_REPORT": "slowRep",
    "CLAIM_CHURN": "churn",
    "REPEAT_ANY": "repeat",
    "REPEAT_FIRST": "repFirst",
    "REPEAT_LATER": "repLater",
    "REPEAT_OWN": "repOwn",
    "OPENS_LEFT": "opensLeft",
}
RATER_CODES = {"REPEAT_ANY": {"DUP_SEEN", "DUP_UNSEEN"}}
LABEL = re.compile(r"(solo|indep|team|claim|scout|share|chief|zones)-([1-9][0-9]*)")
NUMBER = r"\d+(?:\s*(?:-|to)\s*\d+)?"
BOX_LIST = re.compile(
    rf"\b(?:box(?:es)?\"?|open(?:s|ing|ed)?|claim(?:s|ing|ed)?|take|taking)\s*[#=:]?\s*"
    rf"({NUMBER}(?:\s*(?:,|/|and|&|\+|or)\s*{NUMBER})*)"
    r"(?!\d|\s*(?:box|boxes|opens?|more|left|of|rooms?|flags?|agents?)\b)",
    re.I,
)
ROOM_LIST = re.compile(r"\brooms?\"?\s*[#=:]?\s*(\d+(?:\s*(?:-|,|/|and|&)\s*\d+)*)", re.I)
EMPTY_WORDS = re.compile(
    r"\bempty\b|flag\"?\s*[=:]?\s*false|\bfalse\b|no\s*flag|noflag|not\s+(?:a\s+)?treasure", re.I
)
RESULT_WORDS = re.compile(r"empty|false|true|flag|treasure|\byes\b|\bno\b", re.I)
CLAUSE = re.compile(r"[.;!\n]+")
READ_KINDS = ("msgs", "new")
GAP_EDGES = (5, 15, 60)
GAP_BINS = ("<5s", "5-15s", "15-60s", ">=60s")
REPEAT_CLASSES = ("DUP_SEEN", "DUP_UNSEEN", "OWN", "NO_POST")
RULES = f"""
Codes for each seat from the world event log (TRIAL/verifier/events.jsonl) alone. The rules are
frozen from forensics-2026-11-24/detect.py. An open is an event of kind "open" with ok true. A post
is an event of kind "msg" with ok true. A read is a GET event of kind "msgs" or "new" with ok true.
GET /msgs returns the whole board; GET /new (family horde) returns only the posts that the seat's
earlier GET /new calls did not return. A read "returned" a post when the post is in the
"messages" of the read's response; a read that returns no post is still a read.
B is the number of boxes (rooms x {ROOM_SIZE}, from the task's instance.json). A text names box b
when BOX_LIST matches a list that holds b and 1 <= b <= B. In a list, "low-high" and "low to high"
mean every box from low to high when low < high and high - low <= {RANGE_SPAN_MAX}; other ranges
name no box. A text names room r when ROOM_LIST matches a list that holds r ("a-b" with a < b is
every room from a to b). A clause is a part of a text between ".", ";", "!" or a new line.

REPEAT_ANY       each open by the seat of a box that any seat (the seat too) opened before.
DUP_SEEN         each repeat open of box b by the seat where an earlier post by another seat names
                 b and a read by the seat after 1 such post and before the open returned that post.
DUP_UNSEEN       each repeat open of box b by the seat where an earlier post by another seat names
                 b and no read by the seat between any such post and the open returned it.
EMPTY_ROOM_OPEN  each open by the seat in a room that it knew to be empty before the open: it
                 opened an empty box of that room before, or an earlier read returned a message
                 from another seat with a clause that matches EMPTY_WORDS and names the room or a
                 box of the room, and some open in the trial found that room empty.
EARLY_STOP       1 when, at the last event of the seat (any kind, any status), the seat has opens
                 left and a room where an open (any seat) found a flag before that event has a box
                 that nobody opened and that no post by a seat with opens left names (posts of
                 other seats, up to that event). A seat without events never has the code.
SLOW_REPORT      each open by the seat that it never reports, or reports only after
                 {SLOW_AFTER_OPENS} or more further opens. A report is the first later post by the
                 seat that holds the box number (not after a digit or ".", not before a digit)
                 and matches RESULT_WORDS, or that has a clause that names the room of the box and
                 matches RESULT_WORDS. Exempt: a reported open of a box that the seat named in a
                 post before the open, that found a flag, in a room with a flag found before.
CLAIM_CHURN      claim arms only. A claim is a post that names 1 to {CLAIM_BOXES_MAX} boxes.
                 Each run of {CHURN_CLAIMS} claims by the seat that each name a box the seat never
                 opens, with no open by the seat inside the run (longer runs count once until the
                 next open), plus each box that the seat claimed first (before the first open of
                 the box) and never opened.
REPEAT_FIRST     each repeat open by another seat of a box that this seat opened first.
REPEAT_LATER     each repeat open by this seat of a box that another seat opened first.
REPEAT_OWN       each repeat open by this seat of a box that it opened first itself.
OPENS_LEFT       the seat ends with fewer opens than its budget; the count is its unused opens.
READ_BEFORE_OPEN share of the seat's opens with a read by the seat after its previous open (the
                 first open: after the start). A measure, not a code; no value at 0 opens.
0-OPEN SEAT      a seat with 0 opens. Arms with such seats are also shown without them.

A seat has a code when its count is above 0. Share = seats with the code / seats. Mean = the mean
count for each seat. Repeats for each trial = REPEAT_ANY summed over the seats / trials.

--timing adds for each arm: the seats whose first open comes before any read; the seats with 2 or
more opens and no read between their first and last open; the median seconds from the seat's last
read to an open (repeats and first opens apart; opens without an earlier read are counted apart);
for each repeat, the seconds since the first open of the box (bins: below {GAP_EDGES[0]}, below
{GAP_EDGES[1]}, below {GAP_EDGES[2]}, from {GAP_EDGES[2]}), its class (DUP_SEEN, DUP_UNSEEN, OWN =
the seat opened the box first, NO_POST = no earlier post by another seat names the box), whether
the first opener posted a report of the box (the SLOW_REPORT report rule) after its open and before
the repeat, and whether a read by the repeating seat after that report returned it. With --trials,
every repeat is listed.

BOX_LIST     = {BOX_LIST.pattern}
ROOM_LIST    = {ROOM_LIST.pattern}
EMPTY_WORDS  = {EMPTY_WORDS.pattern}
RESULT_WORDS = {RESULT_WORDS.pattern}
"""


def expand(token: str) -> set[int]:
    parts = re.split(r"\s*(?:-|to)\s*", token.strip())
    if len(parts) == 1:
        return {int(parts[0])}
    low, high = int(parts[0]), int(parts[1])
    return set(range(low, high + 1)) if low < high and high - low <= RANGE_SPAN_MAX else set()


def boxes_named(text: str, limit: int) -> set[int]:
    found: set[int] = set()
    for match in BOX_LIST.finditer(text):
        for token in re.split(r"\s*(?:,|/|and|&|\+|or)\s*", match.group(1)):
            if token.strip():
                found |= expand(token)
    return {b for b in found if 1 <= b <= limit}


def room_of(box: int) -> int:
    return (box - 1) // ROOM_SIZE + 1


def rooms_named(text: str) -> set[int]:
    rooms: set[int] = set()
    for match in ROOM_LIST.finditer(text):
        for token in re.split(r"\s*(?:,|/|and|&)\s*", match.group(1)):
            parts = [int(n) for n in re.findall(r"\d+", token)]
            ranged = len(parts) == 2 and parts[0] < parts[1]
            rooms |= set(range(parts[0], parts[-1] + 1)) if ranged else set(parts)
    return rooms


def rooms_said_empty(text: str, limit: int) -> set[int]:
    said: set[int] = set()
    for clause in CLAUSE.split(text):
        if EMPTY_WORDS.search(clause):
            said |= rooms_named(clause) | {room_of(b) for b in boxes_named(clause, limit)}
    return said


def reports_open(text: str, box: int) -> bool:
    if re.search(rf"(?<![\d.]){box}(?!\d)", text) and RESULT_WORDS.search(text):
        return True
    return any(
        room_of(box) in rooms_named(c) and RESULT_WORDS.search(c) for c in CLAUSE.split(text)
    )


def instance_of(trial: Path) -> dict:
    result = json.loads((trial / "result.json").read_text())
    task = Path(result["config"]["task"]["path"])
    path = task / "environment" / "world" / "instance.json"
    assert path.is_file(), (trial, path)
    inst = json.loads(path.read_text())
    assert inst["room_size"] == ROOM_SIZE, (path, inst["room_size"])
    assert 0 < len(inst["seats"]) <= MAX_SEATS and inst["budget"] > 0, path
    return inst


def load_world(trial: Path) -> dict:
    lines = (trial / "verifier" / "events.jsonl").read_text().splitlines()
    assert 0 < len(lines) <= MAX_EVENTS, trial
    events = sorted((json.loads(line) for line in lines if line.strip()), key=lambda e: e["seq"])
    assert events, trial
    inst = instance_of(trial)
    opens = [e for e in events if e["kind"] == "open" and e["ok"]]
    posts = [
        (e["seq"], e["response"]["id"], e["seat"], e["response"]["text"])
        for e in events
        if e["kind"] == "msg" and e["ok"]
    ]
    return {
        "trial": trial,
        "events": events,
        "opens": opens,
        "posts": posts,
        "seats": list(inst["seats"]),
        "budget": inst["budget"],
        "limit": inst["rooms"] * ROOM_SIZE,
        "empty_rooms": {e["response"]["room"] for e in opens if not e["response"]["flag"]},
    }


def is_read(e: dict) -> bool:
    return e["kind"] in READ_KINDS and e["method"] == "GET" and e["ok"]


def seat_reads(w: dict, seat: str) -> list[tuple[int, float, set[int]]]:
    return [
        (e["seq"], e["t"], {m["id"] for m in e["response"]["messages"]})
        for e in w["events"]
        if e.get("seat") == seat and is_read(e)
    ]


def dup_post(w: dict, seat: str, e: dict, reads: list) -> tuple[str, tuple] | None:
    box = e["request"]["box"]
    naming = [
        p
        for p in w["posts"]
        if p[0] < e["seq"] and p[2] != seat and box in boxes_named(p[3], w["limit"])
    ]
    if not naming:
        return None
    seen = [p for p in naming if any(p[0] < r[0] < e["seq"] and p[1] in r[2] for r in reads)]
    return ("DUP_SEEN" if seen else "DUP_UNSEEN"), (seen or naming)[0]


def detect_dups(w: dict, seat: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"DUP_SEEN": [], "DUP_UNSEEN": [], "REPEAT_ANY": []}
    opened: set[int] = set()
    reads = seat_reads(w, seat)
    for e in w["opens"]:
        box = e["request"]["box"]
        if e["seat"] == seat and box in opened:
            found["REPEAT_ANY"].append(f"box {box} at seq {e['seq']}")
            dup = dup_post(w, seat, e, reads)
            if dup is not None:
                code, post = dup
                found[code].append(
                    f"box {box} at seq {e['seq']}; named in #{post[1]} by {post[2]} at seq {post[0]}"
                )
        opened.add(box)
    return found


def detect_empty_room(w: dict, seat: str) -> list[str]:
    known: dict[int, str] = {}
    hits = []
    for e in w["events"]:
        if e.get("seat") != seat or not e["ok"]:
            continue
        if is_read(e):
            for m in e["response"]["messages"]:
                for room in rooms_said_empty(m["text"], w["limit"]) & w["empty_rooms"]:
                    if m["from"] != seat:
                        known.setdefault(room, f"#{m['id']} by {m['from']}, read at seq {e['seq']}")
        elif e["kind"] == "open":
            room = e["response"]["room"]
            if room in known:
                hits.append(
                    f"box {e['request']['box']} (room {room}) at seq {e['seq']}; "
                    f"room known empty from {known[room]}"
                )
            if not e["response"]["flag"]:
                known.setdefault(room, f"own open of box {e['request']['box']} at seq {e['seq']}")
    return hits


def detect_early_stop(w: dict, seat: str) -> list[str]:
    own = [e["seq"] for e in w["events"] if e.get("seat") == seat]
    stop = own[-1] if own else 0
    budget, limit = w["budget"], w["limit"]
    before = [e for e in w["opens"] if e["seq"] <= stop]
    used = Counter(e["seat"] for e in before)
    left = budget - used[seat]
    if left <= 0:
        return []
    flag_rooms = {e["response"]["room"] for e in before if e["response"]["flag"]}
    opened = {e["request"]["box"] for e in before}
    live: set[int] = set()
    for seq, _, author, text in w["posts"]:
        if seq <= stop and author != seat and budget - used[author] > 0:
            live |= boxes_named(text, limit)
    free = sorted(
        b
        for r in flag_rooms
        for b in range((r - 1) * ROOM_SIZE + 1, r * ROOM_SIZE + 1)
        if b not in opened and b not in live
    )
    more = "..." if len(free) > 6 else ""
    return [f"{left} opens left at seq {stop}; free flag boxes {free[:6]}{more}"] if free else []


def detect_slow_report(w: dict, seat: str) -> list[str]:
    mine = [e for e in w["opens"] if e["seat"] == seat]
    own = [p for p in w["posts"] if p[2] == seat]
    hits = []
    for n, e in enumerate(mine):
        box = e["request"]["box"]
        flag_rooms_before = {
            o["response"]["room"]
            for o in w["opens"]
            if o["seq"] < e["seq"] and o["response"]["flag"]
        }
        announced = any(p[0] < e["seq"] and box in boxes_named(p[3], w["limit"]) for p in own)
        report = next((p for p in own if p[0] > e["seq"] and reports_open(p[3], box)), None)
        exempt = e["response"]["flag"] and e["response"]["room"] in flag_rooms_before
        if report and announced and exempt:
            continue
        later = [o for o in mine[n + 1 :] if report is None or o["seq"] < report[0]]
        if report is None:
            hits.append(f"box {box} at seq {e['seq']} never reported")
        elif len(later) >= SLOW_AFTER_OPENS:
            hits.append(
                f"box {box} at seq {e['seq']} reported in #{report[1]} after {len(later)} more opens"
            )
    return hits


def detect_claim_churn(w: dict, seat: str) -> list[str]:
    limit = w["limit"]
    mine = [e for e in w["opens"] if e["seat"] == seat]
    opened_by_seat = {e["request"]["box"] for e in mine}
    first_open: dict[int, int] = {}
    for e in w["opens"]:
        first_open.setdefault(e["request"]["box"], e["seq"])
    claims = [p for p in w["posts"] if 1 <= len(boxes_named(p[3], limit)) <= CLAIM_BOXES_MAX]
    hits = []
    run: list[int] = []
    marks = sorted([(e["seq"], None) for e in mine] + [(p[0], p) for p in claims if p[2] == seat])
    for _, post in marks:
        if post is None:
            run = []
        elif boxes_named(post[3], limit) - opened_by_seat:
            run.append(post[1])
            if len(run) == CHURN_CLAIMS:
                hits.append(
                    f"{CHURN_CLAIMS} claims in a row without an open: #{run[0]} to #{run[-1]}"
                )
    first_claim: dict[int, tuple[str, int]] = {}
    for seq, number, author, text in claims:
        for b in boxes_named(text, limit):
            if seq < first_open.get(b, float("inf")):
                first_claim.setdefault(b, (author, number))
    for b, (author, number) in sorted(first_claim.items()):
        if author == seat and b not in opened_by_seat:
            hits.append(f"gave up box {b}, claimed first in #{number}")
    return hits


def repeat_roles(w: dict) -> dict[str, Counter]:
    first: dict[int, str] = {}
    roles: dict[str, Counter] = {seat: Counter() for seat in w["seats"]}
    for e in w["opens"]:
        box, seat = e["request"]["box"], e["seat"]
        assert seat in roles, (w["trial"], seat)
        if box not in first:
            first[box] = seat
        elif first[box] == seat:
            roles[seat]["REPEAT_OWN"] += 1
        else:
            roles[seat]["REPEAT_LATER"] += 1
            roles[first[box]]["REPEAT_FIRST"] += 1
    later = sum(c["REPEAT_LATER"] for c in roles.values())
    assert later == sum(c["REPEAT_FIRST"] for c in roles.values()), w["trial"]
    return roles


def read_before_open(w: dict) -> dict[str, tuple[int, int]]:
    read = dict.fromkeys(w["seats"], False)
    counts = {seat: [0, 0] for seat in w["seats"]}
    for e in w["events"]:
        seat = e.get("seat")
        if seat not in read or not e["ok"]:
            continue
        if is_read(e):
            read[seat] = True
        elif e["kind"] == "open":
            counts[seat][0] += read[seat]
            counts[seat][1] += 1
            read[seat] = False
    return {seat: (c[0], c[1]) for seat, c in counts.items()}


def repeat_detail(w: dict, e: dict, first: dict, reads: list) -> dict:
    seat, box = e["seat"], e["request"]["box"]
    dup = dup_post(w, seat, e, reads)
    own = first["seat"] == seat
    report = next(
        (
            p
            for p in w["posts"]
            if p[2] == first["seat"] and first["seq"] < p[0] < e["seq"] and reports_open(p[3], box)
        ),
        None,
    )
    read_report = report is not None and any(
        report[0] < r[0] < e["seq"] and report[1] in r[2] for r in reads
    )
    gap = e["t"] - first["t"]
    assert gap >= 0, (w["trial"], e["seq"])
    return {
        "first_seat": first["seat"],
        "first_seq": first["seq"],
        "gap_s": gap,
        "class": "OWN" if own else (dup[0] if dup else "NO_POST"),
        "named_in": dup[1][1] if dup else None,
        "report": None if own or report is None else report[1],
        "report_read": None if own or report is None else read_report,
    }


def seat_timing(w: dict, reads: dict) -> dict[str, dict]:
    out = {}
    for seat in w["seats"]:
        seqs = [e["seq"] for e in w["opens"] if e["seat"] == seat]
        if not seqs:
            continue
        between = [r for r in reads[seat] if seqs[0] < r[0] < seqs[-1]]
        out[seat] = {
            "opens": len(seqs),
            "first_open_before_read": not any(r[0] < seqs[0] for r in reads[seat]),
            "no_read_between": not between if len(seqs) >= 2 else None,
        }
    return out


def trial_timing(w: dict) -> dict:
    reads = {seat: seat_reads(w, seat) for seat in w["seats"]}
    first: dict[int, dict] = {}
    opens = []
    for e in w["opens"]:
        seat, box = e["seat"], e["request"]["box"]
        before = [r for r in reads[seat] if r[0] < e["seq"]]
        row = {
            "seat": seat,
            "box": box,
            "seq": e["seq"],
            "flag": e["response"]["flag"],
            "repeat": box in first,
            "since_read_s": e["t"] - before[-1][1] if before else None,
        }
        if box in first:
            row |= repeat_detail(w, e, first[box], reads[seat])
        else:
            first[box] = e
        opens.append(row)
    return {"opens": opens, "seats": seat_timing(w, reads)}


def detect_seat(w: dict, seat: str, claim: bool) -> dict[str, list[str]]:
    found = detect_dups(w, seat)
    found["EMPTY_ROOM_OPEN"] = detect_empty_room(w, seat)
    found["EARLY_STOP"] = detect_early_stop(w, seat)
    found["SLOW_REPORT"] = detect_slow_report(w, seat)
    if claim:
        found["CLAIM_CHURN"] = detect_claim_churn(w, seat)
    return found


def trial_seats(w: dict, claim: bool) -> list[dict]:
    trial = w["trial"]
    roles = repeat_roles(w)
    reads = read_before_open(w)
    rows = []
    for seat in w["seats"]:
        found = detect_seat(w, seat, claim)
        with_read, opens = reads[seat]
        assert opens <= w["budget"], (trial, seat, opens)
        counts = {code: len(found[code]) if code in found else None for code in JUDGED}
        counts |= {
            code: roles[seat][code] for code in ("REPEAT_FIRST", "REPEAT_LATER", "REPEAT_OWN")
        }
        counts["REPEAT_ANY"] = len(found["REPEAT_ANY"])
        counts["OPENS_LEFT"] = w["budget"] - opens
        own = roles[seat]["REPEAT_OWN"] + roles[seat]["REPEAT_LATER"]
        assert counts["REPEAT_ANY"] == own, (trial, seat)
        rows.append(
            {
                "trial": trial.name,
                "seat": seat,
                "budget": w["budget"],
                "opens": opens,
                "zero_opens": opens == 0,
                "read_before_open": with_read / opens if opens else None,
                "reads_before_opens": with_read,
                "counts": counts,
                "evidence": {k: v for k, v in found.items() if v},
            }
        )
    return rows


def arm_kind(label: str) -> str:
    match = LABEL.search(label)
    assert match, f"{label}: the label must hold solo|indep|team|claim|scout|share|chief|zones-N"
    return match.group(1)


def load_arm(text: str, timing: bool) -> dict:
    label, sep, path = text.partition("=")
    assert sep and label and path, f"--arm wants LABEL=DIR: {text}"
    job = Path(path).expanduser()
    assert job.is_dir(), f"{label}: {job} is not a directory"
    kind = arm_kind(label)
    trials = sorted(p for p in job.iterdir() if "__" in p.name and (p / "result.json").is_file())
    assert 0 < len(trials) <= MAX_TRIALS, f"{job}: {len(trials)} trial folders"
    unscored = [t.name for t in trials if not (t / "verifier" / "reward.json").is_file()]
    rows = []
    for t in trials:
        if t.name in unscored:
            continue
        w = load_world(t)
        row = {"trial": t.name, "seats": trial_seats(w, kind == "claim")}
        rows.append(row | {"timing": trial_timing(w)} if timing else row)
    return {"label": label, "dir": str(job), "kind": kind, "unscored": unscored, "trials": rows}


def summarize(label: str, seats: list[dict], trials: int) -> dict:
    assert seats and trials > 0, label
    codes = {}
    for code in CODES:
        values = [s["counts"][code] for s in seats]
        if any(v is None for v in values):
            assert all(v is None for v in values), (label, code)
            codes[code] = None
            continue
        with_code = sum(v > 0 for v in values)
        codes[code] = {
            "seats": with_code,
            "share": with_code / len(seats),
            "mean": fmean(values),
            "total": sum(values),
        }
    shares = [s["read_before_open"] for s in seats if s["read_before_open"] is not None]
    opens = sum(s["opens"] for s in seats)
    return {
        "label": label,
        "trials": trials,
        "seats": len(seats),
        "zero_open_seats": sum(s["zero_opens"] for s in seats),
        "opens": opens,
        "repeats_per_trial": codes["REPEAT_ANY"]["total"] / trials,
        "unused_opens": codes["OPENS_LEFT"]["total"],
        "codes": codes,
        "read_before_open_mean": fmean(shares) if shares else None,
        "read_before_open_pooled": sum(s["reads_before_opens"] for s in seats) / opens
        if opens
        else None,
        "seats_with_opens": len(shares),
    }


def summaries(arm: dict) -> list[dict]:
    seats = [s for t in arm["trials"] for s in t["seats"]]
    found = [summarize(arm["label"], seats, len(arm["trials"]))]
    active = [s for s in seats if not s["zero_opens"]]
    if len(active) < len(seats):
        dropped = len(seats) - len(active)
        found.append(summarize(f"{arm['label']} w/o {dropped} 0-open", active, len(arm["trials"])))
    return found


def cell(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_arm(arm: dict) -> list[str]:
    lines = []
    for s in arm["summaries"]:
        lines += [
            f"== {s['label']}  ({arm['dir']})",
            f"trials {s['trials']}, seats {s['seats']}, seats with 0 opens {s['zero_open_seats']}, "
            f"opens {s['opens']}, repeats for each trial {s['repeats_per_trial']:.2f}, "
            f"unused opens {s['unused_opens']}, unscored trials {len(arm['unscored'])}",
            f"{'code':<17}{'seats':>6}{'share':>8}{'mean/seat':>11}",
        ]
        for code in CODES:
            c = s["codes"][code]
            if c is None:
                lines.append(f"{code:<17}{'-':>6}{'-':>8}{'-':>11}  (not in scope)")
                continue
            lines.append(f"{code:<17}{c['seats']:>6}{c['share']:>8.3f}{c['mean']:>11.3f}")
        lines.append(
            f"{'READ_BEFORE_OPEN':<17} mean over {s['seats_with_opens']} seats with opens "
            f"{cell(s['read_before_open_mean'], 3)}, pooled over opens "
            f"{cell(s['read_before_open_pooled'], 3)}"
        )
        lines.append("")
    zero = [
        f"{t['trial']}/{x['seat']}" for t in arm["trials"] for x in t["seats"] if x["zero_opens"]
    ]
    if zero:
        lines += [f"seats with 0 opens in {arm['label']} ({len(zero)}):", *zero, ""]
    return lines


def render_matrix(arms: list[dict]) -> list[str]:
    head = f"{'arm':<28}{'seats':>6}{'rep/tr':>8}" + "".join(f"{SHORT[c]:>10}" for c in CODES)
    lines = [
        "share of seats with each code (rep/tr = repeats for each trial; readB = READ_BEFORE_OPEN mean)"
    ]
    lines.append(head + f"{'readB':>8}")
    for arm in arms:
        for s in arm["summaries"]:
            cells = "".join(
                f"{cell(s['codes'][c]['share'] if s['codes'][c] else None, 3):>10}" for c in CODES
            )
            lines.append(
                f"{s['label']:<28}{s['seats']:>6}{s['repeats_per_trial']:>8.2f}{cells}"
                f"{cell(s['read_before_open_mean'], 3):>8}"
            )
    return [*lines, ""]


def render_trials(arm: dict) -> list[str]:
    lines = [
        f"-- trials of {arm['label']}: number of seats with each code",
        f"{'trial':<30}{'seats':>6}{'opens':>6}{'0open':>6}"
        + "".join(f"{SHORT[c]:>10}" for c in CODES),
    ]
    for t in arm["trials"]:
        seats = t["seats"]
        cells = "".join(
            f"{'-' if seats[0]['counts'][c] is None else sum(s['counts'][c] > 0 for s in seats):>10}"
            for c in CODES
        )
        opens = sum(s["opens"] for s in seats)
        zero = sum(s["zero_opens"] for s in seats)
        lines.append(f"{t['trial']:<30}{len(seats):>6}{opens:>6}{zero:>6}{cells}")
    return [*lines, ""]


def gap_bin(gap: float) -> str:
    return GAP_BINS[sum(gap >= edge for edge in GAP_EDGES)]


def share(part: int, whole: int) -> str:
    return f"{part}/{whole} = {part / whole:.3f}" if whole else f"{part}/0"


def timing_summary(arm: dict) -> dict:
    opens = [o for t in arm["trials"] for o in t["timing"]["opens"]]
    seats = [s for t in arm["trials"] for s in t["timing"]["seats"].values()]
    repeats = [o for o in opens if o["repeat"]]
    others = [o for o in repeats if o["class"] != "OWN"]
    multi = [s for s in seats if s["no_read_between"] is not None]
    since = {
        kind: [
            o["since_read_s"]
            for o in opens
            if o["repeat"] is kind and o["since_read_s"] is not None
        ]
        for kind in (True, False)
    }
    by_bin = {
        b: {
            "repeats": sum(gap_bin(o["gap_s"]) == b for o in others),
            "reported": sum(gap_bin(o["gap_s"]) == b and o["report"] is not None for o in others),
            "not_read": sum(gap_bin(o["gap_s"]) == b and o["report_read"] is False for o in others),
        }
        for b in GAP_BINS
    }
    found = {
        "trials": len(arm["trials"]),
        "opens": len(opens),
        "first_opens": len(opens) - len(repeats),
        "seats_with_opens": len(seats),
        "first_open_before_read": sum(s["first_open_before_read"] for s in seats),
        "seats_two_opens": len(multi),
        "no_read_between": sum(s["no_read_between"] for s in multi),
        "median_since_read_repeat": median(since[True]) if since[True] else None,
        "median_since_read_first": median(since[False]) if since[False] else None,
        "repeats_with_read": len(since[True]),
        "first_opens_with_read": len(since[False]),
        "repeats": len(repeats),
        "classes": {c: sum(o["class"] == c for o in repeats) for c in REPEAT_CLASSES},
        "gap_bins": {b: sum(gap_bin(o["gap_s"]) == b for o in repeats) for b in GAP_BINS},
        "other_seat_repeats": len(others),
        "reported_before": sum(o["report"] is not None for o in others),
        "report_not_read": sum(o["report_read"] is False for o in others),
        "other_by_gap_bin": by_bin,
    }
    assert sum(found["classes"].values()) == len(repeats), arm["label"]
    assert found["classes"]["OWN"] == len(repeats) - len(others), arm["label"]
    return found


def render_timing(arm: dict, listed: bool) -> list[str]:
    s = arm["timing"]
    rep, first = s["repeats"], s["first_opens"]
    lines = [
        f"-- timing of {arm['label']}: {s['trials']} trials, {s['opens']} opens, {rep} repeats",
        f"seats whose first open comes before any read: "
        f"{share(s['first_open_before_read'], s['seats_with_opens'])}",
        f"seats with 2 or more opens and no read between the first and last open: "
        f"{share(s['no_read_between'], s['seats_two_opens'])}",
        f"median s from the seat's last read to the open: repeats "
        f"{cell(s['median_since_read_repeat'], 1)} ({s['repeats_with_read']}/{rep} with a read), "
        f"first opens {cell(s['median_since_read_first'], 1)} ({s['first_opens_with_read']}/{first})",
        "repeat classes: " + ", ".join(f"{c} {n}" for c, n in s["classes"].items()),
        "s since the first open of the box: "
        + ", ".join(f"{b} {n}" for b, n in s["gap_bins"].items()),
        f"repeats of a box that another seat opened first: {s['other_seat_repeats']}; "
        f"first opener reported it before {share(s['reported_before'], s['other_seat_repeats'])}; "
        f"reported but the repeater read no report {share(s['report_not_read'], s['other_seat_repeats'])}",
        "by s since the first open (repeats / reported / reported, not read): "
        + ", ".join(
            f"{b} {v['repeats']}/{v['reported']}/{v['not_read']}"
            for b, v in s["other_by_gap_bin"].items()
        ),
    ]
    if listed and rep:
        lines.append("trial seq seat box first_seat gap_s class named_in report report_read")
        lines += [
            f"{t['trial']} {o['seq']} {o['seat']} {o['box']} {o['first_seat']} {o['gap_s']:.1f} "
            f"{o['class']} {o['named_in']} {o['report']} {o['report_read']}"
            for t in arm["trials"]
            for o in t["timing"]["opens"]
            if o["repeat"]
        ]
    return [*lines, ""]


def kappa(pairs: list[tuple[bool, bool]]) -> float:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[v] * right[v] for v in (True, False)) / (n * n)
    return (observed - expected) / (1 - expected) if expected < 1 else float("nan")


def rater_votes(items: dict, ratings: dict, item: str, code: str) -> int:
    a, b = items[item]["raters"]
    wanted = RATER_CODES.get(code, {code})
    return bool(wanted & set(ratings[a][item]["codes"])) + bool(
        wanted & set(ratings[b][item]["codes"])
    )


def agreement_lines(items: dict, found: dict, ratings: dict) -> list[str]:
    head = f"{'code':<16} {'truth':<6} {'n':>3} {'TP':>3} {'FP':>3} {'FN':>3} {'TN':>3}"
    lines = [head + f" {'prec':>5} {'recall':>6} {'kappa':>6}"]
    for code in (*JUDGED, "REPEAT_ANY"):
        scope = [
            i
            for i, row in items.items()
            if code != "CLAIM_CHURN" or arm_kind(row["arm"]) == "claim"
        ]
        for mode, need in (("any", 1), ("both", 2)):
            pairs = [
                (bool(found[i].get(code)), rater_votes(items, ratings, i, code) >= need)
                for i in scope
            ]
            tp = sum(d and t for d, t in pairs)
            fp = sum(d and not t for d, t in pairs)
            fn = sum(t and not d for d, t in pairs)
            tn = len(pairs) - tp - fp - fn
            precision = tp / (tp + fp) if tp + fp else float("nan")
            recall = tp / (tp + fn) if tp + fn else float("nan")
            lines.append(
                f"{code:<16} {mode:<6} {len(scope):>3} {tp:>3} {fp:>3} {fn:>3} {tn:>3} "
                f"{precision:>5.2f} {recall:>6.2f} {kappa(pairs):>6.2f}"
            )
    lines.append(
        "REPEAT_ANY = any repeat open by the seat (event log), against raters' DUP_SEEN or DUP_UNSEEN"
    )
    return lines


def check_raters(study: Path) -> tuple[list[str], dict]:
    items = json.loads((study / "sample-key.json").read_text())["items"]
    assert 0 < len(items) <= MAX_TRIALS, study
    ratings = {
        r: {
            e["id"]: e
            for e in json.loads((study / "ratings" / f"rater-{r}.json").read_text())["ratings"]
        }
        for r in RATERS
    }
    found = {}
    for item, row in sorted(items.items()):
        w = load_world(study.parent / row["arm_path"] / row["trial"])
        assert w["budget"] == row["budget"] and row["seat"] in w["seats"], (item, row)
        found[item] = detect_seat(w, row["seat"], arm_kind(row["arm"]) == "claim")
    lines = agreement_lines(items, found, ratings)
    reference = (study / "detect.txt").read_text().splitlines()[: len(lines)]
    diff = list(difflib.unified_diff(reference, lines, "detect.txt", "seat_codes.py", lineterm=""))
    verdict = "identical to" if not diff else "DIFFERS from"
    out = [f"rater agreement on {len(items)} items, {verdict} {study / 'detect.txt'}", *lines]
    out += [*diff, ""]
    return out, {"items": len(items), "identical": not diff, "table": lines, "diff": diff}


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seat_codes.py",
        description="Behaviour codes for each seat of muster-boxes arms, from the event log alone.",
        epilog=RULES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--arm", action="append", default=[], metavar="LABEL=DIR")
    parser.add_argument("--trials", action="store_true", help="also print a table for each trial")
    parser.add_argument("--timing", action="store_true", help="also print read and repeat timing")
    parser.add_argument("--json", type=Path, metavar="FILE")
    parser.add_argument("--check-raters", type=Path, metavar="STUDY_DIR")
    args = parser.parse_args(argv)
    if not args.arm and args.check_raters is None:
        parser.error("give --arm LABEL=DIR or --check-raters STUDY_DIR")
    if len(args.arm) > MAX_ARMS:
        parser.error(f"at most {MAX_ARMS} arms")
    return args


def main(argv: list[str]) -> int:
    args = parse(argv)
    arms = [load_arm(text, args.timing) for text in args.arm]
    lines: list[str] = []
    for arm in arms:
        arm["summaries"] = summaries(arm)
        if args.timing:
            arm["timing"] = timing_summary(arm)
    if arms:
        lines += render_matrix(arms)
    for arm in arms:
        lines += render_arm(arm) + (render_trials(arm) if args.trials else [])
        lines += render_timing(arm, args.trials) if args.timing else []
    raters = None
    if args.check_raters is not None:
        found, raters = check_raters(args.check_raters.expanduser())
        lines += found
    print("\n".join(lines))
    if args.json is not None:
        report = {"rules": RULES, "arms": arms, "raters": raters}
        args.json.write_text(json.dumps(report, indent=1) + "\n")
    return 0 if raters is None or raters["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
