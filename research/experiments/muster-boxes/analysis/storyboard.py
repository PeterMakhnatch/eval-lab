import argparse
import html
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
MAX_EVENTS = 100_000
MAX_SEATS = 64
MAX_TRIALS = 1000
TEXT_CHARS = 200
SHORT_CHARS = 90
REWARD_TOLERANCE = 1e-6
NO_TOKEN = "no token"
SEAT = re.compile(r"p([1-9][0-9]*)")
TICK_STEPS = (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400)
LAYER = {"refused": 0, "read": 1, "post": 2, "open": 3}
OPEN_TEXT = {"flag": "new flag", "empty": "new empty box", "repeat": "repeat"}
PAGE = {"left": 64, "plot": 1100, "right": 250, "strip": 78, "axis": 30, "space": 600}
PAGE |= {"min": 12, "max": 36, "ticks": 12, "font": 11, "titled": True}
CARD = {"left": 34, "plot": 440, "right": 8, "strip": 0, "axis": 16, "space": 120}
CARD |= {"min": 3, "max": 22, "ticks": 6, "font": 9, "titled": False}
BY_BOX = ("own", "unposted_box", "unread_box", "read_box")
F13 = ("own", "unsent", "unread", "read")
CSS = """
:root { color-scheme: light dark; --bg: #ffffff; --fg: #1b1b1b; --muted: #666666;
  --grid: #d4d4d4; --zebra: #f3f3f3; --flag: #E69F00; --empty: #0072B2; --repeat: #CC79A7;
  --post: #009E73; --read: #4d4d4d; --refused: #D55E00; }
@media (prefers-color-scheme: dark) { :root { --bg: #151515; --fg: #ececec; --muted: #a8a8a8;
  --grid: #3c3c3c; --zebra: #1f1f1f; --empty: #56B4E9; --read: #c8c8c8; } }
body { background: var(--bg); color: var(--fg); font: 14px/1.45 system-ui, sans-serif;
  margin: 18px; }
h1 { font-size: 20px; margin: 0 0 4px 0; }
h2 { font-size: 16px; margin: 22px 0 6px 0; }
.sub { color: var(--muted); margin: 0 0 10px 0; }
.stats { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 8px 0 12px 0; }
.stats b { font-size: 16px; }
svg text { fill: var(--fg); font-family: system-ui, sans-serif; }
svg .muted { fill: var(--muted); }
.zebra { fill: var(--zebra); }
.idle { stroke: var(--grid); stroke-dasharray: 2 3; }
.active { stroke: var(--muted); stroke-width: 1.5; }
.gridline { stroke: var(--grid); stroke-width: 0.6; }
.open.flag .fill { fill: var(--flag); stroke: var(--fg); stroke-width: 0.6; }
.open.flag .ring { fill: none; stroke: var(--flag); stroke-width: 1.4; }
.open.empty .fill { fill: var(--empty); }
.open.repeat .fill { fill: var(--repeat); }
.open.repeat .slash { stroke: var(--bg); stroke-width: 1.6; }
.post line { stroke: var(--post); stroke-width: 2; }
.read circle { fill: none; stroke: var(--read); stroke-width: 1; }
.refused line { stroke: var(--refused); stroke-width: 1.5; }
.link { fill: none; stroke: var(--repeat); stroke-width: 1; stroke-dasharray: 3 2; }
.flagline { fill: none; stroke: var(--flag); stroke-width: 2; }
.axisline { stroke: var(--muted); stroke-width: 0.8; }
table { border-collapse: collapse; font-size: 13px; }
th, td { border-bottom: 1px solid var(--grid); padding: 3px 8px; text-align: left;
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td { white-space: nowrap; }
td.text { white-space: normal; min-width: 280px; }
.cards { display: flex; flex-wrap: wrap; gap: 14px; }
.card { border: 1px solid var(--grid); border-radius: 6px; padding: 6px 8px; }
.card .name { font-size: 12px; margin: 0 0 2px 0; }
"""


def load_module(name: str, path: Path) -> ModuleType:
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cs = load_module("storyboard_crowd_scaling", HERE / "crowd_scaling.py")


def seat_key(seat: str) -> int:
    found = SEAT.fullmatch(seat)
    assert found is not None, seat
    return int(found.group(1))


def clean(text: str) -> str:
    assert isinstance(text, str)
    kept = [
        ch
        if ch in "\t\n\r" or 0x20 <= ord(ch) < 0xD800 or 0xE000 <= ord(ch) <= 0xFFFD or ord(ch) > 0xFFFF
        else "?"
        for ch in text
    ]
    return "".join(kept)


def esc(text) -> str:
    return html.escape(clean(str(text)), quote=True)


def short(text: str, limit: int) -> str:
    assert limit > 1
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def num(value: float) -> str:
    return f"{value:.1f}"


def read_events(trial: Path) -> list[dict]:
    lines = [line for line in (trial / "verifier" / "events.jsonl").read_text().splitlines()]
    lines = [line for line in lines if line.strip()]
    assert 0 < len(lines) <= MAX_EVENTS, (trial, len(lines))
    out = []
    for number, line in enumerate(lines, start=1):
        e = json.loads(line)
        assert isinstance(e, dict) and e.get("seq") == number, (trial, number)
        out.append(e)
        if e["kind"] == "admin.close" and e["ok"]:
            break
    return out


def load_instance(result: dict) -> dict:
    task = ((result.get("config") or {}).get("task") or {}).get("path")
    assert task, "result.json names no task path"
    path = Path(task) / "environment" / "world" / "instance.json"
    assert path.is_file(), path
    inst = json.loads(path.read_text())
    for key in ("seats", "budget", "rooms", "room_size", "treasure_rooms", "family"):
        assert key in inst, (path, key)
    assert 0 < len(inst["seats"]) <= MAX_SEATS, (path, len(inst["seats"]))
    assert inst["budget"] > 0 and inst["rooms"] > 0 and inst["room_size"] > 0, path
    return inst


def treasure_of(inst: dict) -> tuple[int, str]:
    rooms = inst["treasure_rooms"]
    if isinstance(rooms, list):
        assert all(isinstance(r, int) and 0 < r <= inst["rooms"] for r in rooms), rooms
        return len(rooms), ", ".join(str(r) for r in sorted(rooms))
    assert isinstance(rooms, int) and 0 < rooms <= inst["rooms"], rooms
    return rooms, f"{rooms} rooms (numbers not in the instance file)"


def repeat_row(e: dict, first: tuple, kinds: tuple[str, str], posts: list, reads: dict) -> dict:
    box, seat = e["response"]["box"], e["seat"]
    naming = [p for p in posts if box in p["mentions"] and p["seat"] != seat]
    post = naming[0] if naming else None
    read_after = None
    if post is not None:
        read_after = any(seq > post["seq"] for seq in reads.get(seat, []))
    first_seat, first_seq, first_t = first
    assert first_seq < e["seq"] and first_t <= e["t"], (first, e["seq"])
    return {
        "box": box,
        "room": e["response"]["room"],
        "first_seat": first_seat,
        "first_t": first_t,
        "seat": seat,
        "t": e["t"],
        "post": post,
        "read_after": read_after,
        "f13": kinds[0],
        "by_box": kinds[1],
    }


def note_post(e: dict, chan: dict, c: Counter, posts: list, reads: dict) -> None:
    cs.note_message(e, chan, c)
    if e["method"] == "POST":
        text = e["response"]["text"]
        posts.append({"seq": e["seq"], "t": e["t"], "seat": e["seat"], "text": text})
        posts[-1]["mentions"] = cs.mentioned(text)
        return
    reads.setdefault(e["seat"], []).append(e["seq"])


def walk_story(kept: list[dict], budget: int) -> dict:
    first: dict[int, tuple[str, int, float]] = {}
    chan: dict = {"posts": [], "seen": {}, "last_read": {}}
    posts: list[dict] = []
    reads: dict[str, list[int]] = {}
    used: Counter = Counter()
    c: Counter = Counter()
    opens: dict[int, dict] = {}
    repeats: list[dict] = []
    flag_times: list[float] = []
    for e in kept:
        seat, kind, method = e["seat"], e["kind"], e["method"]
        if (kind, method) in (("msg", "POST"), ("msgs", "GET")):
            note_post(e, chan, c, posts, reads)
        elif kind == "open" and method == "POST":
            box = e["response"]["box"]
            assert isinstance(box, int) and e["request"]["box"] == box, e["seq"]
            used[seat] += 1
            assert e["response"]["budget_left"] == budget - used[seat], (seat, e["seq"])
            if box in first:
                kinds = cs.repeat_kinds(first[box][:2], box, e, chan)
                opens[e["seq"]] = {"result": "repeat", "first": first[box]}
                repeats.append(repeat_row(e, first[box], kinds, posts, reads))
                continue
            first[box] = (seat, e["seq"], e["t"])
            flag = bool(e["response"]["flag"])
            opens[e["seq"]] = {"result": "flag" if flag else "empty"}
            flag_times.extend([e["t"]] if flag else [])
    story = {"opens": opens, "repeats": repeats, "flag_times": flag_times, "posts": posts}
    return story | {"messages": c["messages"], "reads": c["reads"]}


def check_story(story: dict, c: Counter) -> None:
    results = Counter(o["result"] for o in story["opens"].values())
    assert len(story["opens"]) == c["opens"], (len(story["opens"]), c["opens"])
    assert results["flag"] == c["flags"] and results["empty"] == c["empty"], results
    assert results["repeat"] == c["repeats"] == len(story["repeats"]), results
    by_box = Counter(r["by_box"] for r in story["repeats"])
    f13 = Counter(r["f13"] for r in story["repeats"])
    for key in BY_BOX[1:]:
        assert by_box[key] == c["repeat_" + key], (key, by_box, c)
    for key in F13:
        assert f13[key] == c["repeat_" + key], (key, f13, c)
    assert story["messages"] == c["messages"] and story["reads"] == c["reads"]


def lane_stats(events: list[dict], seats: list[str], result: dict, t0: float) -> list[dict]:
    errors = {}
    for item in result.get("seat_results") or []:
        info = item.get("exception_info") or {}
        errors[item.get("seat")] = info.get("exception_type")
    lanes = {s: {"name": s, "error": errors.get(s), "times": [], "count": Counter()} for s in seats}
    for e in events:
        seat = e["seat"]
        if seat is None:
            continue
        assert seat in lanes, (e["seq"], seat)
        lanes[seat]["times"].append(e["t"] - t0)
        kind = "refused" if not e["ok"] or e.get("late") else f"{e['kind']} {e['method']}"
        lanes[seat]["count"][kind] += 1
    return [lanes[s] for s in seats]


def event_mark(e: dict, story: dict) -> dict | None:
    if e["kind"] == "admin.close" and e["ok"]:
        return None
    if not e["ok"] or e.get("late"):
        return {"kind": "refused", "lane": e["seat"] or NO_TOKEN, "e": e}
    if e["seat"] is None:
        return None
    key = (e["kind"], e["method"])
    if key == ("open", "POST"):
        return {"kind": "open", "lane": e["seat"], "e": e} | story["opens"][e["seq"]]
    if key == ("msg", "POST"):
        return {"kind": "post", "lane": e["seat"], "e": e}
    if key == ("msgs", "GET"):
        return {"kind": "read", "lane": e["seat"], "e": e}
    return None


def trial_model(trial: Path) -> dict:
    result = json.loads((trial / "result.json").read_text())
    assert isinstance(result, dict), trial
    inst = load_instance(result)
    seats = sorted(inst["seats"], key=seat_key)
    assert len(set(seats)) == len(seats), seats
    events = read_events(trial)
    kept = cs.played(trial)
    story = walk_story(kept, inst["budget"])
    check_story(story, cs.walk(kept, inst["budget"])[0])
    treasure_count, treasure_text = treasure_of(inst)
    total_flags = treasure_count * inst["room_size"]
    reward = cs.read_reward(trial, result)
    found = len(story["flag_times"])
    if reward is not None:
        assert abs(reward - found / total_flags) < REWARD_TOLERANCE, (trial, reward, found)
    t0 = events[0]["t"]
    marks = [m for m in (event_mark(e, story) for e in events) if m is not None]
    lanes = lane_stats(events, seats, result, t0)
    anonymous = sum(1 for m in marks if m["lane"] == NO_TOKEN)
    if anonymous:
        lanes.append({"name": NO_TOKEN, "error": None, "times": [], "count": Counter()})
        lanes[-1]["count"]["refused"] = anonymous
    return {
        "trial": trial,
        "name": trial.name,
        "arm": "/".join(trial.resolve().parent.parts[-3:]),
        "inst": inst,
        "seats": seats,
        "story": story,
        "marks": marks,
        "lanes": lanes,
        "t0": t0,
        "span": max(events[-1]["t"] - t0, 1.0),
        "reward": reward,
        "found": found,
        "total_flags": total_flags,
        "treasure": treasure_text,
        "anonymous": anonymous,
    }


def geometry(base: dict, lanes: list[dict], span: float) -> dict:
    assert lanes and span > 0
    lane_h = max(base["min"], min(base["max"], base["space"] // len(lanes)))
    height = base["strip"] + lane_h * len(lanes) + base["axis"]
    width = base["left"] + base["plot"] + base["right"]
    rows = {lane["name"]: i for i, lane in enumerate(lanes)}
    return base | {"lane_h": lane_h, "height": height, "width": width, "rows": rows, "span": span}


def gx(g: dict, t: float) -> float:
    return g["left"] + max(0.0, t) / g["span"] * g["plot"]


def gy(g: dict, lane: str) -> float:
    return g["strip"] + g["rows"][lane] * g["lane_h"] + g["lane_h"] / 2


def clock(m: dict, t: float) -> str:
    stamp = datetime.fromtimestamp(t, UTC).strftime("%H:%M:%S.%f")[:-3]
    return f"{t - m['t0']:.3f} s (UTC {stamp})"


def mark_title(mk: dict, m: dict) -> str:
    e = mk["e"]
    who = e["seat"] or NO_TOKEN
    head = f"{who} at {clock(m, e['t'])}, seq {e['seq']}"
    if mk["kind"] == "open":
        r = e["response"]
        text = f"{head}: open box {r['box']} in room {r['room']}: {OPEN_TEXT[mk['result']]}"
        if mk["result"] == "repeat":
            seat, seq, t = mk["first"]
            text += f"; first opened by {seat} at {t - m['t0']:.3f} s (seq {seq})"
        return text
    if mk["kind"] == "post":
        body = e["response"]["text"]
        return f"{head}: post #{e['response']['id']}: {body[:TEXT_CHARS]}"
    if mk["kind"] == "read":
        got = e["response"].get("messages") or []
        newest = max((x.get("id", 0) for x in got), default=0)
        return f"{head}: read the board ({len(got)} messages, newest id {newest})"
    error = (e.get("response") or {}).get("error") if isinstance(e.get("response"), dict) else None
    late = " (late)" if e.get("late") else ""
    detail = str(error if error is not None else e.get("response"))[:TEXT_CHARS]
    return f"{head}: refused{late} {e['method']} /{e['kind']} status {e['status']}: {detail}"


def open_svg(result: str, x: float, y: float, r: float) -> str:
    fill = f'<circle class="fill" cx="{num(x)}" cy="{num(y)}" r="{num(r)}"/>'
    if result == "flag":
        ring = f'<circle class="ring" cx="{num(x)}" cy="{num(y)}" r="{num(r + max(1.2, r * 0.4))}"/>'
        return ring + fill
    if result == "repeat":
        d = r * 0.75
        line = f'x1="{num(x - d)}" y1="{num(y + d)}" x2="{num(x + d)}" y2="{num(y - d)}"'
        return fill + f'<line class="slash" {line}/>'
    return fill


def shape_svg(kind: str, result: str | None, x: float, y: float, g: dict) -> str:
    h = g["lane_h"]
    r = max(2.0, min(6.0, h * 0.22))
    if kind == "open":
        assert result in OPEN_TEXT, result
        return open_svg(result, x, y, r)
    if kind == "post":
        return f'<line x1="{num(x)}" y1="{num(y - h * 0.46)}" x2="{num(x)}" y2="{num(y - h * 0.1)}"/>'
    low = y + h * 0.28
    if kind == "read":
        return f'<circle cx="{num(x)}" cy="{num(low)}" r="{num(max(1.4, r * 0.55))}"/>'
    assert kind == "refused", kind
    d = max(1.5, r * 0.6)
    a = f'<line x1="{num(x - d)}" y1="{num(low - d)}" x2="{num(x + d)}" y2="{num(low + d)}"/>'
    b = f'<line x1="{num(x - d)}" y1="{num(low + d)}" x2="{num(x + d)}" y2="{num(low - d)}"/>'
    return a + b


def mark_svg(mk: dict, m: dict, g: dict) -> str:
    x, y = gx(g, mk["e"]["t"] - m["t0"]), gy(g, mk["lane"])
    result = mk.get("result")
    css = f"{mk['kind']} {result}" if result else mk["kind"]
    title = f"<title>{esc(mark_title(mk, m))}</title>" if g["titled"] else ""
    box = f' data-box="{mk["e"]["response"]["box"]}"' if mk["kind"] == "open" else ""
    return f'<g class="{css}"{box}>{title}{shape_svg(mk["kind"], result, x, y, g)}</g>'


def link_svg(mk: dict, m: dict, g: dict) -> str:
    seat, _, t = mk["first"]
    x1, y1 = gx(g, t - m["t0"]), gy(g, seat)
    x2, y2 = gx(g, mk["e"]["t"] - m["t0"]), gy(g, mk["lane"])
    bow = g["lane_h"] * 0.45
    mx, my = (x1 + x2) / 2, min(y1, y2) - bow
    path = f"M{num(x1)},{num(y1)} Q{num(mx)},{num(my)} {num(x2)},{num(y2)}"
    return f'<path class="link" d="{path}"/>'


def tick_step(span: float, limit: int) -> int:
    assert span > 0 and limit > 1
    for step in TICK_STEPS:
        if span / step <= limit:
            return step
    return TICK_STEPS[-1] * (int(span / TICK_STEPS[-1] / limit) + 1)


def axis_svg(g: dict) -> list[str]:
    step = tick_step(g["span"], g["ticks"])
    bottom = g["height"] - g["axis"]
    out = [f'<line class="axisline" x1="{g["left"]}" y1="{bottom}" x2="{g["left"] + g["plot"]}" y2="{bottom}"/>']
    count = int(g["span"] // step) + 1
    assert count <= g["ticks"] + 1, (count, step)
    for i in range(count):
        x = num(gx(g, i * step))
        out.append(f'<line class="gridline" x1="{x}" y1="0" x2="{x}" y2="{bottom}"/>')
        label = f"{i * step} s" if i else "0"
        out.append(f'<text x="{x}" y="{bottom + g["font"] + 3}" font-size="{g["font"]}" text-anchor="middle" class="muted">{label}</text>')
    if g["titled"]:
        out.append(f'<text x="{g["left"] + g["plot"]}" y="{bottom + 2 * g["font"] + 6}" font-size="{g["font"]}" text-anchor="end" class="muted">seconds since the first event of the trial</text>')
    return out


def lane_text(lane: dict) -> str:
    c = lane["count"]
    if lane["name"] == NO_TOKEN:
        return f"{c['refused']} refused"
    if not lane["times"]:
        return "no requests"
    text = f"{c['open POST']} open · {c['msg POST']} post · {c['msgs GET']} read"
    return text + (f" · {lane['error']}" if lane["error"] else "")


def lane_title(lane: dict, m: dict) -> str:
    c = lane["count"]
    if lane["name"] == NO_TOKEN:
        return f"{c['refused']} refused requests without a valid seat token (the seat is unknown)"
    if not lane["times"]:
        return f"{lane['name']}: no requests"
    first, last = min(lane["times"]), max(lane["times"])
    parts = ", ".join(f"{n} {k}" for k, n in sorted(c.items()))
    error = f"; seat error {lane['error']}" if lane["error"] else ""
    return f"{lane['name']}: requests from {first:.1f} s to {last:.1f} s, silent after {last:.1f} s; {parts}{error}"


def lane_svg(i: int, lane: dict, m: dict, g: dict) -> list[str]:
    h, y = g["lane_h"], gy(g, lane["name"])
    top, right = g["strip"] + i * h, g["left"] + g["plot"]
    out = [f'<rect class="zebra" x="0" y="{top}" width="{g["width"]}" height="{h}"/>'] if i % 2 else []
    out.append(f'<line class="idle" x1="{g["left"]}" y1="{num(y)}" x2="{right}" y2="{num(y)}"/>')
    if lane["times"]:
        x1, x2 = gx(g, min(lane["times"])), gx(g, max(lane["times"]))
        out.append(f'<line class="active" x1="{num(x1)}" y1="{num(y)}" x2="{num(x2)}" y2="{num(y)}"/>')
    font = min(g["font"], max(5, int(h * 0.8)))
    css = ' class="muted"' if lane["error"] or not lane["times"] or lane["name"] == NO_TOKEN else ""
    title = f"<title>{esc(lane_title(lane, m))}</title>" if g["titled"] else ""
    label = esc(lane["name"]) + ("!" if lane["error"] else "")
    if h >= 5:
        out.append(f'<text x="{g["left"] - 6}" y="{num(y + font * 0.35)}" font-size="{font}" text-anchor="end"{css}>{title}{label}</text>')
    if g["right"] >= 100:
        out.append(f'<text x="{right + 8}" y="{num(y + font * 0.35)}" font-size="{font}"{css}>{esc(lane_text(lane))}</text>')
    return out


def level(n: int, total: int, top: float, base: float) -> float:
    assert 0 <= n <= total and total > 0
    return base - (base - top) * n / total


def strip_svg(m: dict, g: dict) -> list[str]:
    total = m["total_flags"]
    top, base = 8.0, g["strip"] - 14.0
    points = [f"M{num(g['left'])},{num(base)}"]
    for n, t in enumerate(sorted(m["story"]["flag_times"]), start=1):
        points.append(f"H{num(gx(g, t - m['t0']))} V{num(level(n, total, top, base))}")
    points.append(f"H{num(g['left'] + g['plot'])}")
    right = g["left"] + g["plot"]
    font = g["font"]
    return [
        f'<line class="axisline" x1="{g["left"]}" y1="{num(base)}" x2="{right}" y2="{num(base)}"/>',
        f'<line class="gridline" x1="{g["left"]}" y1="{num(top)}" x2="{right}" y2="{num(top)}"/>',
        f'<path class="flagline" d="{" ".join(points)}"><title>distinct flag boxes over time: {m["found"]} of {total}</title></path>',
        f'<text x="{g["left"] - 6}" y="{num(top + 4)}" font-size="{font}" text-anchor="end" class="muted">{total}</text>',
        f'<text x="{g["left"] - 6}" y="{num(base + 4)}" font-size="{font}" text-anchor="end" class="muted">0</text>',
        f'<text x="{right + 8}" y="{num(level(m["found"], total, top, base) + 4)}" font-size="{font}">{m["found"]} / {total} flag boxes</text>',
        f'<text x="{g["left"] + 4}" y="{num(top + font + 2)}" font-size="{font}" class="muted">distinct flag boxes found</text>',
    ]


def chart_svg(m: dict, g: dict) -> str:
    label = esc(f"{m['name']}: lanes of {len(m['seats'])} seats")
    head = f'<svg xmlns="http://www.w3.org/2000/svg" class="chart" data-trial="{esc(m["name"])}"'
    head += f' viewBox="0 0 {g["width"]} {g["height"]}" width="{g["width"]}" height="{g["height"]}"'
    head += f' style="max-width:100%;height:auto" role="img" aria-label="{label}">'
    parts = [head]
    for i, lane in enumerate(lane for lane in m["lanes"] if lane["name"] in g["rows"]):
        parts.extend(lane_svg(i, lane, m, g))
    parts.extend(axis_svg(g))
    if g["strip"]:
        parts.extend(strip_svg(m, g))
    shown = [mk for mk in m["marks"] if mk["lane"] in g["rows"]]
    parts.extend(link_svg(mk, m, g) for mk in shown if mk.get("result") == "repeat")
    parts.extend(mark_svg(mk, m, g) for mk in sorted(shown, key=lambda mk: LAYER[mk["kind"]]))
    parts.append("</svg>")
    return "".join(parts)


def legend_svg() -> str:
    g = PAGE | {"lane_h": 24, "titled": False}
    items = [
        ("open", "flag", "open: new flag box (ring)"),
        ("open", "empty", "open: new empty box"),
        ("open", "repeat", "open: repeat of an opened box (slash; dashed line back to the first open)"),
        ("post", None, "post to the board (tick above the lane)"),
        ("read", None, "board read, GET /msgs (hollow dot below)"),
        ("refused", None, "refused request (cross below)"),
    ]
    out = ['<svg xmlns="http://www.w3.org/2000/svg" class="legend" viewBox="0 0 1380 60" width="1380" height="60" style="max-width:100%;height:auto">']
    for i, (kind, result, text) in enumerate(items):
        x, y = 14 + (i % 3) * 460, 16 + (i // 3) * 28
        css = f"{kind} {result}" if result else kind
        out.append(f'<g class="{css}">{shape_svg(kind, result, x, y, g)}</g>')
        out.append(f'<text x="{x + 14}" y="{y + 4}" font-size="12">{esc(text)}</text>')
    out.append("</svg>")
    return "".join(out)


def stat_html(m: dict) -> str:
    opens = len(m["story"]["opens"])
    results = Counter(o["result"] for o in m["story"]["opens"].values())
    budget = len(m["seats"]) * m["inst"]["budget"]
    refused = sum(1 for mk in m["marks"] if mk["kind"] == "refused" and mk["lane"] != NO_TOKEN)
    reward = "unmeasured" if m["reward"] is None else f"{m['reward']:.3f}"
    items = [
        (reward, "reward"),
        (f"{opens} / {budget}", "opens used"),
        (results["flag"], "new flags"),
        (results["empty"], "new empty"),
        (results["repeat"], "repeats"),
        (budget - opens, "unused opens"),
        (m["story"]["messages"], "messages"),
        (m["story"]["reads"], "board reads"),
        (refused, "refused (seat known)"),
        (m["anonymous"], "refused (no seat token)"),
        (m["treasure"], "treasure rooms"),
    ]
    return "".join(f"<span><b>{esc(v)}</b> {esc(k)}</span>" for v, k in items)


def page_head(title: str) -> list[str]:
    return [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>",
    ]


def when(seconds: float) -> str:
    return f"{seconds:.1f} s"


def repeat_cells(r: dict, m: dict) -> list[str]:
    post = r["post"]
    if post is None:
        named, read = "none", "—"
    else:
        named = f"{post['seat']} at {when(post['t'] - m['t0'])}: {short(post['text'], SHORT_CHARS)}"
        read = "yes" if r["read_after"] else "no"
    return [
        str(r["box"]),
        str(r["room"]),
        f"{r['first_seat']} at {when(r['first_t'] - m['t0'])}",
        f"{r['seat']} at {when(r['t'] - m['t0'])}",
        f"{r['t'] - r['first_t']:.1f}",
        named,
        read,
        f"{r['by_box']} / {r['f13']}",
    ]


def repeat_table(m: dict) -> list[str]:
    rows = m["story"]["repeats"]
    out = [f"<h2>Repeats ({len(rows)})</h2>"]
    if not rows:
        return [*out, "<p>No box was opened twice.</p>"]
    heads = [
        "box", "room", "first open", "repeat", "seconds between",
        "earlier post by another seat that names the box", "repeater read the board after that post",
        "crowd_scaling class (box / report)",
    ]
    out.append("<table><thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in heads) + "</tr></thead><tbody>")
    for r in rows:
        attrs = f'data-repeat="{r["box"]}" data-by-box="{r["by_box"]}" data-f13="{r["f13"]}"'
        cells = "".join(
            f'<td class="text">{esc(c)}</td>' if i == 5 else f"<td>{esc(c)}</td>"
            for i, c in enumerate(repeat_cells(r, m))
        )
        out.append(f"<tr {attrs}>{cells}</tr>")
    out.append("</tbody></table>")
    out.append(
        "<p class=\"sub\">A post names a box when the box number occurs as a number in its text "
        "(crowd_scaling.mentioned). The class column is the crowd_scaling.py walk: box class looks at "
        "posts by the first opener that name the box (unposted_box, unread_box, read_box); report class "
        "looks at any post by the first opener after the first open (unsent, unread, read); own means "
        "the same seat opened the box twice.</p>"
    )
    return out


def trial_page(m: dict) -> str:
    inst = m["inst"]
    g = geometry(PAGE, m["lanes"], m["span"])
    start = datetime.fromtimestamp(m["t0"], UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    sub = (
        f"{m['arm']} · family {inst['family']} · condition {inst.get('condition', '?')} · "
        f"{len(m['seats'])} seat{'' if len(m['seats']) == 1 else 's'} × {inst['budget']} opens · "
        f"{inst['rooms']} rooms × {inst['room_size']} boxes"
        f" · board {'on' if inst.get('channel') else 'off'} · time 0 = {start} · span {m['span']:.1f} s"
    )
    errors = [lane["name"] for lane in m["lanes"] if lane["error"]]
    note = f"Seats with an agent error (label ends in !): {', '.join(errors)}." if errors else ""
    out = page_head(f"{m['name']} storyboard")
    out += [f"<h1>{esc(m['name'])}</h1>", f'<p class="sub">{esc(sub)}</p>']
    out += [f'<div class="stats">{stat_html(m)}</div>', legend_svg()]
    out += [f'<p class="sub">Dotted lane: the seat made no request in that time. Solid lane: from its first to its last request. {esc(note)} Hover a mark for its exact time and content.</p>']
    out += [chart_svg(m, g), *repeat_table(m), "</body></html>"]
    return "\n".join(out)


def card_html(m: dict, span: float) -> str:
    lanes = [lane for lane in m["lanes"] if lane["name"] != NO_TOKEN]
    g = geometry(CARD, lanes, span)
    reward = "unmeasured" if m["reward"] is None else f"{m['reward']:.3f}"
    repeats = len(m["story"]["repeats"])
    name = f"{m['name']} · reward {reward} · {repeats} repeats"
    return f'<div class="card"><p class="name">{esc(name)}</p>{chart_svg(m, g)}</div>'


def arm_page(arm: Path) -> str:
    trials = cs.trial_dirs(arm)
    assert len(trials) <= MAX_TRIALS, arm
    models = [trial_model(t) for t in trials]
    models.sort(key=lambda m: (m["reward"] is None, -(m["reward"] or 0.0), m["name"]))
    span = max(m["span"] for m in models)
    label = "/".join(arm.resolve().parts[-3:])
    rewards = [m["reward"] for m in models if m["reward"] is not None]
    mean = f"{sum(rewards) / len(rewards):.3f}" if rewards else "unmeasured"
    sub = (
        f"{len(models)} trials, sorted by reward (highest first) · mean reward {mean} · "
        f"same time scale on every chart: 0 to {span:.0f} s · seat p1 at the top · "
        "refused requests without a seat token are left out"
    )
    out = page_head(f"{label} overview")
    out += [f"<h1>{esc(label)}</h1>", f'<p class="sub">{esc(sub)}</p>', legend_svg()]
    out.append('<div class="cards">')
    out.extend(card_html(m, span) for m in models)
    out += ["</div>", "</body></html>"]
    return "\n".join(out)


def expected(trial: Path) -> Counter:
    result = json.loads((trial / "result.json").read_text())
    inst = load_instance(result)
    kept = cs.played(trial)
    c = cs.walk(kept, inst["budget"])[0]
    raw = sum(1 for e in read_events(trial) if e["kind"] == "open" and e["method"] == "POST" and e["ok"] and e["seat"] and not e.get("late"))
    assert raw == c["opens"], (trial, raw, c["opens"])
    return c


def check_chart(chart: ET.Element, c: Counter) -> Counter:
    shown: Counter = Counter()
    for el in chart.iter():
        names = (el.get("class") or "").split()
        if "open" in names:
            assert len(names) == 2 and names[1] in OPEN_TEXT, names
            shown[names[1]] += 1
    assert sum(shown.values()) == c["opens"], (shown, c["opens"])
    assert (shown["flag"], shown["empty"], shown["repeat"]) == (c["flags"], c["empty"], c["repeats"])
    return shown


def check_rows(root: ET.Element, c: Counter) -> int:
    rows = [el for el in root.iter() if el.get("data-repeat") is not None]
    assert len(rows) == c["repeats"], (len(rows), c["repeats"])
    by_box = Counter(el.get("data-by-box") for el in rows)
    f13 = Counter(el.get("data-f13") for el in rows)
    assert by_box["own"] == c["repeat_own"], (by_box, c)
    for key in BY_BOX[1:]:
        assert by_box[key] == c["repeat_" + key], (key, by_box, c)
    for key in F13:
        assert f13[key] == c["repeat_" + key], (key, f13, c)
    return len(rows)


def check(path: Path, trials: list[Path], page: bool) -> list[str]:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    charts = {el.get("data-trial"): el for el in root.iter() if el.get("data-trial")}
    assert set(charts) == {t.name for t in trials}, (sorted(charts), len(trials))
    lines = []
    for trial in trials:
        c = expected(trial)
        shown = check_chart(charts[trial.name], c)
        rows = check_rows(root, c) if page else None
        table = f" repeat rows {rows} match the walk" if page else ""
        lines.append(f"ok {trial.name}: open marks {sum(shown.values())} = successful opens {c['opens']} (flag {shown['flag']}, empty {shown['empty']}, repeat {shown['repeat']}){table}")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="storyboard.py", description="Draw muster trial storyboards as HTML.")
    parser.add_argument("trial", nargs="?", type=Path, help="trial folder")
    parser.add_argument("--arm", type=Path, help="arm folder: one small chart per trial")
    parser.add_argument("--out", type=Path, help="HTML file to write")
    parser.add_argument("--check", type=Path, help="HTML file to check against the event logs")
    args = parser.parse_args(argv)
    if (args.trial is None) == (args.arm is None):
        parser.error("give exactly one of TRIAL_DIR or --arm ARM_DIR")
    if (args.out is None) == (args.check is None):
        parser.error("give exactly one of --out FILE or --check FILE")
    source = args.trial if args.trial is not None else args.arm
    if not source.is_dir():
        parser.error(f"not a folder: {source}")
    trials = [args.trial] if args.trial is not None else cs.trial_dirs(args.arm)
    if args.check is not None:
        print("\n".join(check(args.check, trials, args.trial is not None)))
        return 0
    text = trial_page(trial_model(args.trial)) if args.trial is not None else arm_page(args.arm)
    ET.fromstring(text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
