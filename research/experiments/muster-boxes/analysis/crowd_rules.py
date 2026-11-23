import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from statistics import fmean
from types import ModuleType

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
SEAT_MARK = re.compile(r"(?<![A-Za-z0-9_])(?:(?:agent|seat)\s+#?\s*p?|p_?)([1-9])(?![0-9])", re.I)
ROOM_REF = re.compile(
    r"\brooms?\s*#?\s*[1-6](?![0-9])(?:\s*(?:,|&|/|and|or)\s*[1-6](?![0-9]))*", re.I
)
COUNT_AFTER = re.compile(
    r"(?<![0-9])[0-9]{1,2}\s*(?:opens?\b|boxes\b|left\b|x\b|times\b|flags?\b|seats?\b|agents?\b"
    r"|%|sec\w*|s\b|mins?\b|minutes?\b)",
    re.I,
)
COUNT_BEFORE = re.compile(
    r"\b(?:budget|left|total|seq|id|step|turn|first|last|next)\s*[:=]?\s*[0-9]{1,2}", re.I
)
RANGE = re.compile(r"(?<![0-9])([0-9]{1,2})\s*(?:-|–|—|to|through|\.\.)\s*([0-9]{1,2})(?![0-9])")
NUMBER = re.compile(r"(?<![0-9.])[0-9]{1,2}(?![0-9])(?!\.[0-9])")
LIST_WORD = re.compile(r"\b(?:lists?|assign\w*|allocat\w*|plan)\b", re.I)
RESULT_WORD = re.compile(r"\b(?:flags?|empty|found|treasure|results?|opened)\b", re.I)
LINE_END = re.compile(r"\n|\.\s+(?=[A-Za-z])")
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


def box_numbers(segment: str) -> list[int]:
    clean = COUNT_BEFORE.sub(" ", COUNT_AFTER.sub(" ", ROOM_REF.sub(" ", segment[:MAX_TEXT])))
    found: list[int] = []
    for lo, hi in RANGE.findall(clean):
        a, b = int(lo), int(hi)
        if 1 <= a < b <= BOXES and b - a < MAX_RANGE:
            found.extend(range(a, b + 1))
    found.extend(int(n) for n in NUMBER.findall(RANGE.sub(" ", clean)))
    return list(dict.fromkeys(b for b in found if 1 <= b <= BOXES))


def seat_key(key: str, seats: list[str]) -> str | None:
    match = SEAT_MARK.fullmatch(key.strip())
    name = f"p{match.group(1)}" if match else None
    return name if name in seats else None


def seat_boxes(mapping: dict, seats: list[str]) -> dict[str, list[int]] | None:
    found: dict[str, list[int]] = {}
    for key, value in list(mapping.items())[:MAX_MARKS]:
        seat = seat_key(str(key), seats)
        if seat is None:
            continue
        items = value if isinstance(value, list) else [value]
        boxes = []
        for item in items[:BOXES]:
            if type(item) is int and 1 <= item <= BOXES:
                boxes.append(item)
            elif isinstance(item, str):
                boxes.extend(box_numbers(item))
        found[seat] = list(dict.fromkeys(boxes))
    return found if any(found.values()) else None


def json_list(text: str, seats: list[str]) -> dict[str, list[int]] | None:
    candidates = [text]
    lo, hi = text.find("{"), text.rfind("}")
    if 0 <= lo < hi:
        candidates.append(text[lo : hi + 1])
    for candidate in candidates:
        value = ref.text_json(candidate)
        if not isinstance(value, dict):
            continue
        inner = next((value[k] for k in WRAPPERS if isinstance(value.get(k), dict)), value)
        found = seat_boxes(inner, seats)
        if found is not None:
            return found
    return None


def text_list(text: str, seats: list[str]) -> dict[str, list[int]] | None:
    marks = [m for m in SEAT_MARK.finditer(text[:MAX_TEXT]) if f"p{m.group(1)}" in seats]
    found: dict[str, list[int]] = {}
    for index, mark in enumerate(marks[:MAX_MARKS]):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        segment = text[mark.end() : end]
        boxes = box_numbers(LINE_END.split(segment, maxsplit=1)[0]) or box_numbers(segment)
        seat = f"p{mark.group(1)}"
        found[seat] = list(dict.fromkeys(found.get(seat, []) + boxes))
    found = {seat: boxes for seat, boxes in found.items() if boxes}
    listed = LIST_WORD.search(text) is not None
    plain = RESULT_WORD.search(text) is None
    if len(found) >= 2 and (listed or plain):
        return found
    return found if len(found) == 1 and (listed or (plain and seats[0] not in found)) else None


def parse_list(text: str, seats: list[str]) -> tuple[dict[str, list[int]] | None, str]:
    assert isinstance(text, str) and seats
    exact = ref.list_of(text)
    if exact is not None:
        return {seat: list(boxes) for seat, boxes in exact.items()}, "json"
    if ref.report_of(text) is not None:
        return None, ""
    loose = json_list(text, seats)
    if loose is not None:
        return loose, "loose json"
    found = text_list(text, seats)
    return (found, "text") if found is not None else (None, "")


def looks_like_list(text: str) -> bool:
    if ref.report_of(text) is not None:
        return False
    return '"list"' in text or (LIST_WORD.search(text) is not None and len(box_numbers(text)) >= 2)


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
    if state["unparsed"]:
        return "list not parsed"
    lists = state["lists"]
    if not lists:
        return "before any list existed"
    if box in lists[-1].get(seat, ()):
        return "in latest list"
    if any(box in older.get(seat, ()) for older in lists[:-1]):
        return "in an older list only"
    return "in no list"


def note_list(e: dict, seats: list[str], state: dict, extra: Counter, info: dict) -> None:
    text = e["request"]["text"]
    lists, how = parse_list(text, seats)
    if lists is None:
        if looks_like_list(text):
            extra["p1 posts that look like a list but did not parse"] += 1
            state["unparsed"] = True
            info["cross_check"] = False
        return
    extra["lists posted by p1"] += 1
    extra[f"lists parsed from {how}"] += 1
    info["cross_check"] = info["cross_check"] and how == "json"
    given = Counter(box for boxes in lists.values() for box in set(boxes))
    extra["boxes a list gave to 2 or more seats"] += sum(1 for n in given.values() if n > 1)
    state["lists"].append({seat: set(boxes) for seat, boxes in lists.items()})
    state["unparsed"] = False
    if info["first_list_sec"] is None:
        info["first_list_sec"] = e["t"] - info["start"]


def chief_trial(events: list[dict], inst: dict) -> tuple[list, Counter, dict]:
    seats = sorted(inst["seats"])
    chief = seats[0]
    state: dict = {"lists": [], "unparsed": False}
    info: dict = {"cross_check": True, "first_list_sec": None, "start": events[0]["t"]}
    out, extra = [], Counter()
    for e in events:
        if is_post(e) and e["seat"] == chief:
            note_list(e, seats, state, extra, info)
        if is_open(e):
            out.append((e["seat"], chief_class(e["seat"], e["request"]["box"], state)))
            extra["opens before p1's first parsed list"] += not state["lists"]
    info.pop("start")
    return out, extra, info


CLASSIFIERS = {"share": share_trial, "zones": zones_trial, "chief": chief_trial}


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


def classify(row: dict, events: list[dict], inst: dict, trial: Path) -> dict:
    rule = inst["rule"]
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
        "classes": Counter(cls for _, cls in out),
        "seat_allowed": Counter(seat for seat, cls in out if status[cls] is True),
        "seat_classified": Counter(seat for seat, cls in out if status[cls] is not None),
        "extra": extra,
    }


def trial_row(trial: Path, as_rule: str | None) -> dict:
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
    return row | classify(row, events, inst, trial)


def instance_obedience(rows: list[dict]) -> dict[str, float]:
    allowed: Counter = Counter()
    classified: Counter = Counter()
    for r in rows:
        allowed[r["instance"]] += r["allowed"]
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
    per_instance = instance_obedience(rows)
    values = list(per_instance.values())
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
        "instance_mean": fmean(values) if values else None,
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


def render(arms: list[dict], as_rule: str | None) -> str:
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
    parser.add_argument("--as-rule", choices=("share", "zones"))
    parser.add_argument("--expect-obedient", action="store_true")
    parser.add_argument("--json", metavar="OUT")
    args = parser.parse_args(argv)
    arms = []
    for label, jobs in parse_arms(args.arm).items():
        rows = [trial_row(t, args.as_rule) for job in jobs for t in trial_dirs(job)]
        arms.append(summarize(label, rows))
    print(render(arms, args.as_rule))
    if args.json:
        settings = {"as_rule": args.as_rule, "arms": args.arm}
        Path(args.json).write_text(
            json.dumps({"settings": settings, "arms": arms}, indent=2) + "\n"
        )
    return obedient(arms) if args.expect_obedient else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
