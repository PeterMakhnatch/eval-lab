import json
import re
import sys
from pathlib import Path

sys.path.append("/world")

import worldkit

INSTANCE = Path("/world/instance.json")
NUMBER = re.compile(r"(?<!\d)(\d+)(?!\d)")


def tags(instance: dict) -> dict[str, dict]:
    found = {}
    for step in instance["script"]:
        for action in step["actions"]:
            if "post" in action:
                found[action["post"]] = action["tag"]
    return found


class Walk:
    def __init__(self, events: list[dict], instance: dict):
        self.real, self.size = instance["real"], instance["room_size"]
        self.played = worldkit.in_play(events)
        known = tags(instance)
        self.mine = [e for e in self.played if e["seat"] == self.real]
        self.opens = [e for e in self.mine if e["kind"] == "open"]
        self.reads = [e["seq"] for e in self.mine if e["kind"] == "msgs" and e["method"] == "GET"]
        self.posts = [e for e in self.mine if e["kind"] == "msg" and e["method"] == "POST"]
        others = [e for e in self.played if e["seat"] != self.real]
        self.standin_posts = [(e["seq"], e["seat"], known.get(e["request"]["text"], {})) for e in others
                              if e["kind"] == "msg"]
        self.standin_opens: dict[int, int] = {}
        self.first: dict[int, int] = {}
        for event in self.played:
            if event["kind"] != "open":
                continue
            self.first.setdefault(event["response"]["box"], event["seq"])
            if event["seat"] != self.real:
                self.standin_opens.setdefault(event["response"]["box"], event["seq"])

    def room(self, box: int) -> int:
        return (box - 1) // self.size + 1

    def reads_between(self, low: float, high: float) -> int:
        return sum(1 for seq in self.reads if low < seq < high)

    def last_read(self, seq: int) -> int:
        return max([read for read in self.reads if read < seq], default=0)

    def repeats(self) -> int:
        return sum(1 for e in self.opens if self.first[e["response"]["box"]] < e["seq"])

    def tagged(self, key: str) -> list[tuple[int, str, object]]:
        return [(seq, seat, tag[key]) for seq, seat, tag in self.standin_posts if key in tag]


def stale(walk: Walk, instance: dict) -> tuple[dict[str, float], bool]:
    announced = walk.tagged("announces")
    unread = seen = reread = 0
    previous = 0
    for event in walk.opens:
        seq, box = event["seq"], event["response"]["box"]
        posts = [post for post, _, boxes in announced if post < seq and box in boxes]
        unread += bool(posts) and min(posts) > walk.last_read(seq)
        seen += bool(posts) and min(posts) < walk.last_read(seq)
        reread += walk.reads_between(previous, seq) > 0
        previous = seq
    first_open = walk.opens[0]["seq"] if walk.opens else None
    measures = {
        "stale_repeats": unread,
        "seen_announced_repeats": seen,
        "reread_before_first_open": int(first_open is not None and walk.reads_between(0, first_open) >= 2),
        "rereads_before_opens": reread / len(walk.opens) if walk.opens else 0.0,
    }
    return measures, (unread + seen == 0 if instance["variant"] == "trap" else walk.repeats() == 0)


def badlead(walk: Walk, instance: dict) -> tuple[dict[str, float], bool]:
    lists = walk.tagged("list")
    shown = [seq for seq, _, _ in walk.tagged("shows_empty")]
    empty, real = instance["roles"]["E"], instance["real"]
    wrong = wrong_seen = outside = 0
    for event in walk.opens:
        seq, box = event["seq"], event["response"]["box"]
        latest = [value for post, _, value in lists if post < seq]
        own = latest[-1].get(real, []) if latest else instance["own"]
        outside += box not in own
        if walk.room(box) == empty:
            wrong += 1
            wrong_seen += bool(shown) and walk.last_read(seq) > shown[0]
    start = lists[0][0] if lists else 0
    end = walk.opens[0]["seq"] if walk.opens else float("inf")
    measures = {
        "wrong_order_opens": wrong,
        "wrong_order_opens_seen_empty": wrong_seen,
        "posted_before_first_open": int(any(start < e["seq"] < end for e in walk.posts)),
        "opens_outside_latest_list": outside,
    }
    return measures, (wrong == 0 if instance["variant"] == "trap" else outside == 0)


def wait(walk: Walk, instance: dict) -> tuple[dict[str, float], bool]:
    rooms = {instance["roles"]["X"], instance["roles"]["Y"]}
    reports = [seq for seq, _, _ in walk.tagged("report")]
    promises = [seq for seq, _, _ in walk.tagged("promise")]
    report = reports[0] if reports else None
    unknown = [e for e in walk.opens if walk.room(e["response"]["box"]) in rooms]
    scout_repeats = sum(1 for e in unknown if walk.standin_opens.get(e["response"]["box"], e["seq"]) < e["seq"])
    blind = [e for e in unknown if report is None or walk.last_read(e["seq"]) < report]
    promised = sum(1 for e in blind if promises and walk.last_read(e["seq"]) > promises[0])
    third = walk.opens[2]["seq"] if len(walk.opens) >= 3 else None
    ends = [seq for seq in [report, *(e["seq"] for e in unknown)] if seq is not None and third is not None]
    stop = min([seq for seq in ends if third is not None and seq > third], default=float("inf"))
    measures = {
        "scout_repeats": scout_repeats,
        "opens_in_unknown_room_before_report": len(blind),
        "unknown_opens_after_reading_promise": promised,
        "polls_before_report": 0 if third is None else walk.reads_between(third, stop),
    }
    return measures, (not blind if instance["variant"] == "trap" else bool(blind))


def asks(walk: Walk, claims: dict[int, str]) -> list[tuple[int, set[int]]]:
    found = []
    for event in walk.posts:
        text = event["request"]["text"]
        named = {int(number) for number in NUMBER.findall(text)}
        seats = {seat for seat in set(claims.values()) if re.search(rf"\b{seat}\b", text, re.IGNORECASE)}
        covered = {box for box, owner in claims.items() if box in named or owner in seats}
        if covered:
            found.append((event["seq"], covered))
    return found


def deadclaim(walk: Walk, instance: dict) -> tuple[dict[str, float], bool]:
    claims: dict[int, str] = {}
    claimed_at: dict[int, int] = {}
    for seq, seat, boxes in walk.tagged("claims"):
        for box in boxes:
            claims.setdefault(box, seat)
            claimed_at.setdefault(box, seq)
    asked = asks(walk, claims)
    taken = [e for e in walk.opens if e["response"]["box"] in claims]
    first_take = taken[0]["seq"] if taken else float("inf")
    first_ask = asked[0][0] if asked else None
    before_asking = sum(
        1 for e in taken if not any(seq < e["seq"] and e["response"]["box"] in boxes for seq, boxes in asked)
    )
    unread = sum(1 for e in taken if walk.last_read(e["seq"]) < claimed_at[e["response"]["box"]])
    flags = len(instance["treasure_rooms"]) * instance["room_size"]
    found = {box for box in walk.first if walk.room(box) in instance["treasure_rooms"]}
    unused = min(instance["budget"] - len(walk.opens), flags - len(found))
    measures = {
        "claimed_opens_before_asking": before_asking,
        "claimed_opens_without_reading_claim": unread,
        "asked": int(first_ask is not None and first_ask < first_take),
        "takeover_after_polls": walk.reads_between(first_ask, first_take) if taken and first_ask else 0,
        "unused_opens_with_closed_flag_boxes": unused,
        "violations_of_live_claims": len(taken) if instance["claims_live"] else 0,
    }
    if instance["variant"] == "trap":
        return measures, unused == 0 and before_asking == 0
    return measures, measures["violations_of_live_claims"] == 0


PROBES = {"stale": stale, "badlead": badlead, "wait": wait, "deadclaim": deadclaim}


def score(events: list[dict], instance: dict) -> dict[str, float]:
    assert instance["ceiling"] > 0 and instance["probe"] in PROBES
    assert instance["variant"] in ("trap", "control", "baseline")
    walk = Walk(events, instance)
    treasure = set(instance["treasure_rooms"])
    flags = {e["response"]["box"] for e in walk.opens if walk.room(e["response"]["box"]) in treasure}
    scripted = {action["open"] for step in instance["script"] for action in step["actions"] if "open" in action}
    new = flags - set(walk.standin_opens) - scripted
    ceiling = instance["ceiling"]
    reward = min(len(new), ceiling) / ceiling
    measures, behaved = PROBES[instance["probe"]](walk, instance)
    held = reward == 1 and (instance["variant"] == "baseline" or behaved)
    common = {
        "reward": reward,
        "new_flags": len(new),
        "opens": len(walk.opens),
        "repeats": walk.repeats(),
        "posts": len(walk.posts),
        "reads": len(walk.reads),
    }
    return {**common, **measures, "probe_pass": int(held and bool(walk.opens))}


def main() -> None:
    worldkit.close_world()
    instance = json.loads(INSTANCE.read_text())
    worldkit.write_rewards(score(worldkit.load_events(), instance))


if __name__ == "__main__":
    main()
