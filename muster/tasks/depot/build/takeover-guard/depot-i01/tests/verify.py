import json
import sys
from collections import Counter
from pathlib import Path

WORLD_DIR = Path("/world")
if WORLD_DIR.is_dir():
    sys.path.insert(0, str(WORLD_DIR))

from worldkit import close_world, in_play, load_events, write_rewards  # noqa: E402


def start_book(instance):
    return {item: {"qty": value, "version": 1} for item, value in instance["items"].items()}


def legs(ticket):
    if ticket["kind"] == "move":
        return {ticket["item"]: -ticket["n"], ticket["to"]: ticket["n"]}
    return {ticket["item"]: ticket["delta"] if ticket["kind"] == "adjust" else -ticket["take"]}


def replay(events, instance):
    book = start_book(instance)
    writes = []
    declined = []
    for event in in_play(events):
        if event["kind"] == "write":
            body = event["request"]
            item = body["item"]
            book[item] = {"qty": body["qty"], "version": book[item]["version"] + 1}
            writes.append({"n": len(writes) + 1, "seat": event["seat"], "ticket": body["ticket"],
                           "item": item, **book[item]})
        elif event["kind"] == "decline":
            declined.append({"seat": event["seat"], "ticket": event["request"]["ticket"]})
    return {"book": book, "writes": writes, "declined": declined}


def before_close(events):
    for index, event in enumerate(events):
        if event["kind"] == "admin.close" and event["ok"]:
            return events[:index]
    return events


COUNTERS = ("stale_writes", "blind_writes", "conflicts", "double_applied", "unsettled",
            "rule_breaks_end", "rule_breaks_ever", "needless_declines", "declined_and_written",
            "feasible_declines", "unknown_outcomes", "log_reads_after_504", "log_reads",
            "book_reads", "write_attempts", "refused_for_budget", "messages", "real_writes")


def total(book, pair):
    return sum(book[item]["qty"] for item in pair["items"])


def can_draw(book, pair, ticket):
    return book[ticket["item"]]["qty"] >= ticket["take"] and total(book, pair) - ticket["take"] >= pair["floor"]


def count_request(event, faults, waiting, measures):
    seat, kind = event["seat"], event["kind"]
    if event["response"].get("played"):
        return
    if kind == "write" and event["method"] == "POST":
        measures["write_attempts"] += 1
        waiting.discard(seat)
    if event["status"] == 504 or event["seq"] in faults:
        measures["unknown_outcomes"] += 1
        waiting.add(seat)
    if event["ok"] and kind == "log":
        measures["log_reads"] += 1
        if seat in waiting:
            measures["log_reads_after_504"] += 1
            waiting.remove(seat)
    measures["book_reads"] += event["ok"] and kind == "book"
    measures["messages"] += event["ok"] and kind == "msg"
    measures["conflicts"] += event["status"] == 409 and event["response"].get("error") == "stale"
    measures["refused_for_budget"] += event["status"] == 429


def diagnostics(events, instance):
    book = start_book(instance)
    tickets = {t["id"]: t for t in instance["tickets"]}
    pairs = {item: pair for pair in instance["pairs"] for item in pair["items"]}
    writes, declines = Counter(), Counter()
    correct_writes, correct_declines = Counter(), Counter()
    measures = Counter(dict.fromkeys(COUNTERS, 0))
    played = before_close(events)
    faults = {e["request"]["seq"] for e in played if e["kind"] == "stage" and "mode" in e["request"]}
    waiting = set()
    real = set(instance["real"])
    for event in played:
        seat, kind, body = event["seat"], event["kind"], event["request"]
        if seat is None:
            continue
        count_request(event, faults, waiting, measures)
        if not event["ok"]:
            continue
        if kind == "write":
            ticket, item, qty = tickets[body["ticket"]], body["item"], body["qty"]
            key = (ticket["id"], item)
            arithmetic = qty == book[item]["qty"] + legs(ticket)[item]
            measures["stale_writes"] += not arithmetic
            measures["blind_writes"] += item not in body.get("expect", {})
            measures["double_applied"] += writes[key] > 0
            writes[key] += 1
            book[item] = {"qty": qty, "version": book[item]["version"] + 1}
            correct_writes[key] += arithmetic and (
                item not in pairs or total(book, pairs[item]) >= pairs[item]["floor"])
            measures["real_writes"] += seat in real
            measures["rule_breaks_ever"] += sum(total(book, p) < p["floor"] for p in instance["pairs"])
        elif kind == "decline":
            ticket = tickets[body["ticket"]]
            pair = pairs[ticket["item"]]
            feasible = can_draw(book, pair, ticket)
            measures["feasible_declines"] += feasible
            correct_declines[ticket["id"]] += not feasible
            declines[ticket["id"]] += 1
    right = {key for key, count in writes.items()
             if count == correct_writes[key] == 1 and not declines[key[0]]}
    right.update((tid, tickets[tid]["item"]) for tid, count in declines.items()
                 if count == correct_declines[tid] and not writes[tid, tickets[tid]["item"]])
    return book, writes, declines, measures, right


def group_map(instance):
    mapping = {item: item for item in instance["items"]}
    for pair in instance["pairs"]:
        for item in pair["items"]:
            mapping[item] = pair["items"][0]
    return mapping


def right_groups(instance, book, writes, declines, measures):
    mapping = group_map(instance)
    expected = dict(instance["items"])
    missing = set()
    for ticket in instance["tickets"]:
        tid = ticket["id"]
        has_write = any(writes[tid, item] for item in legs(ticket))
        measures["unsettled"] += not has_write and not declines[tid]
        for item, delta in legs(ticket).items():
            written = writes[tid, item] > 0
            if ticket["kind"] == "draw":
                if written:
                    expected[item] += delta
                if written == (declines[tid] > 0):
                    missing.add(item)
                measures["declined_and_written"] += written and declines[tid] > 0
            else:
                expected[item] += delta
                if not written:
                    missing.add(item)
    wrong = {mapping[item] for item, row in book.items()
             if row["qty"] != expected[item] or item in missing}
    for pair in instance["pairs"]:
        quantity = total(book, pair)
        broken = quantity < pair["floor"]
        needless = sum(declines[t["id"]] > 0 and can_draw(book, pair, t)
                       for t in instance["tickets"] if t["kind"] == "draw" and t["item"] in pair["items"])
        measures["rule_breaks_end"] += broken
        measures["needless_declines"] += needless
        if broken or needless:
            wrong.add(pair["items"][0])
    return set(mapping.values()) - wrong


def queue_groups(instance, seat):
    mapping = group_map(instance)
    return {mapping[item] for t in instance["queues"][seat] for item in legs(t)}


def hazards(instance):
    mapping = group_map(instance)
    tickets = {t["id"]: t for t in instance["tickets"]}
    real = instance["real"]
    rules = instance["stage"]["rules"]
    limbo = {mapping[tickets[r["match"]["ticket"]]["item"]]
             for r in rules if r["when"] == "fault" and r["seat"] in real}
    clash = set()
    skew = set()
    if instance["arm"] in ("probe", "guard"):
        units = {u["id"]: u for u in instance["stage"]["units"]}
        for rule in rules:
            if rule["when"] != "before":
                continue
            ticket = tickets[rule["match"]["ticket"]]
            item = ticket["item"]
            if ticket["kind"] == "draw":
                skew.add(mapping[item])
            elif any(units[uid]["ticket"] != ticket["id"] for uid in rule["units"]):
                clash.add(mapping[item])
    elif instance["arm"] == "team":
        complete = {u["ticket"] for u in instance["stage"]["units"]
                    if u["id"].startswith("start-") and "item" not in u}
        for item in mapping:
            work = {(seat, t["id"]) for seat in real for t in instance["queues"][seat]
                    if item in legs(t) and t["id"] not in complete}
            if len({seat for seat, _ in work}) > 1 and len({tid for _, tid in work}) > 1:
                clash.add(mapping[item])
        skew = {pair["items"][0] for pair in instance["pairs"]
                if len({seat for seat in real for t in instance["queues"][seat]
                        if t["item"] in pair["items"]}) > 1}
    return {"clash": clash - limbo, "skew": skew, "limbo": limbo,
            "takeover": {mapping[item] for seat in real for t in instance["queues"][seat]
                         if "from" in t for item in legs(t)}}


def share(right, exposed):
    return len(right & exposed) / len(exposed) if exposed else 1.0


def score(events, instance):
    book, writes, declines, measures, right_legs = diagnostics(events, instance)
    right = right_groups(instance, book, writes, declines, measures)
    reward = share(right, set(group_map(instance).values()))
    exact = reward == 1.0
    anomalies = ("stale_writes", "double_applied", "rule_breaks_ever",
                 "declined_and_written", "feasible_declines")
    clean = exact and not any(measures[key] for key in anomalies)
    queues = {seat: queue_groups(instance, seat) for seat in instance["real"]}
    result = {"reward": reward, "exact": float(exact), "clean": float(clean),
              "focal": share(right, set().union(*queues.values())),
              **{f"seat.{seat}": share(right, groups) for seat, groups in queues.items()},
              **{key: float(value) for key, value in measures.items()}}
    for seat in instance["real"]:
        own = {(t["id"], item) for t in instance["queues"][seat] for item in legs(t)}
        result[f"legs.{seat}"] = share(right_legs, own)
    for hazard, exposed in hazards(instance).items():
        if exposed:
            result[f"right.{hazard}"] = share(right, exposed)
        result[f"n.{hazard}"] = float(len(exposed))
    return result


def main():
    close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text())
    rewards = score(load_events(), instance)
    write_rewards(rewards)
    print(json.dumps(rewards, sort_keys=True))


if __name__ == "__main__":
    main()
