import json
import sys
from copy import deepcopy
from pathlib import Path

WORLD_DIR = Path("/world")


def deltas(tickets):
    totals = {}
    for ticket in tickets:
        item = ticket["item"]
        if ticket["kind"] == "adjust":
            totals[item] = totals.get(item, 0) + ticket["delta"]
        else:
            assert ticket["kind"] == "move"
            target = ticket["to"]
            totals[item] = totals.get(item, 0) - ticket["n"]
            totals[target] = totals.get(target, 0) + ticket["n"]
    return totals


def initial(instance):
    seats = instance["real"]
    return {"book": dict(instance["book"]), "queues": deepcopy(instance["queues"]),
            "batches": {seat: {} for seat in seats}, "ready": [], "phase": "plan",
            "messages": [], "late_added": False, "requests": dict.fromkeys(seats, 0),
            "sent": dict.fromkeys(seats, 0)}


def replay(events, instance):
    state = initial(instance)
    ready = set()
    for event in events:
        kind, seat, body = event["kind"], event["seat"], event["request"]
        if kind == "admin.close" and event["ok"]:
            break
        if kind == "late.append" and event["ok"]:
            assert body == instance["late"] and not state["late_added"]
            state["queues"][body["seat"]].append(dict(body["ticket"]))
            state["late_added"] = True
            ready.discard(body["seat"])
            continue
        if seat not in state["requests"]:
            continue
        state["requests"][seat] += 1
        if state["phase"] == "done" or event["method"] != "POST":
            continue
        if not event["ok"]:
            if kind == "ready" and event["response"].get("error") == "not current":
                ready.discard(seat)
            continue
        if kind == "msg":
            state["messages"].append({"n": len(state["messages"]) + 1, "from": seat, "text": body["text"]})
            state["sent"][seat] += 1
            ready.clear()
        elif kind == "batch":
            state["batches"][seat] = dict(body["writes"])
            ready.discard(seat)
        elif kind == "ready":
            ready.add(seat)
            if ready == set(instance["real"]):
                for clerk in instance["real"]:
                    state["book"].update(state["batches"][clerk])
                state["phase"] = "done"
    state["ready"] = sorted(ready)
    return state


def ratio(right, total):
    assert 0 <= right <= total
    return right / total if total else 0.0


def book_measures(state, instance):
    truth = dict(instance["book"])
    counts = dict.fromkeys(truth, 0)
    for tickets in state["queues"].values():
        for item, delta in deltas(tickets).items():
            truth[item] += delta
            counts[item] += 1
    right = {item for item in truth if state["book"][item] == truth[item]}
    touched = {item for item in truth if counts[item]}
    shared = {item for item in truth if counts[item] > 1}
    own = touched - shared
    applied = state["batches"].values() if state["phase"] == "done" else []
    writes = {item: [batch[item] for batch in applied if item in batch] for item in truth}
    stale = 0
    if state["late_added"] and state["phase"] == "done":
        ticket = instance["late"]["ticket"]
        stale = sum(state["book"][item] == truth[item] - delta for item, delta in deltas([ticket]).items())
    return {"reward": ratio(len(right), len(truth)), "focal": ratio(len(right & touched), len(touched)),
            "ended": float(state["phase"] == "done"), "right.shared": ratio(len(right & shared), len(shared)),
            "right.own": ratio(len(right & own), len(own)), "unwritten": float(sum(not writes[item] for item in touched)),
            "disagreed": float(sum(len(set(values)) > 1 for values in writes.values())),
            "stale": float(stale), "late_added": float(state["late_added"])}


def diagnostics(events, state, instance):
    measures = {"refused_for_budget": 0.0}
    for seat in instance["real"]:
        measures.update({f"msgs.{seat}": float(state["sent"][seat]),
                         f"chars.{seat}": float(sum(len(m["text"]) for m in state["messages"] if m["from"] == seat)),
                         f"requests.{seat}": float(state["requests"][seat]),
                         f"ready_refused.{seat}": 0.0, f"batches.{seat}": 0.0})
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        seat = event["seat"]
        if seat not in instance["real"]:
            continue
        if event["status"] == 429:
            measures["refused_for_budget"] += 1
        if event["kind"] == "ready" and not event["ok"]:
            measures[f"ready_refused.{seat}"] += 1
        if event["kind"] == "batch" and event["ok"]:
            measures[f"batches.{seat}"] += 1
    return measures


def score(events, instance):
    state = replay(events, instance)
    return {**book_measures(state, instance), **diagnostics(events, state, instance)}


def main():
    sys.path.insert(0, str(WORLD_DIR))
    import worldkit

    worldkit.close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text())
    rewards = score(worldkit.load_events(), instance)
    worldkit.write_rewards(rewards)
    print(json.dumps(rewards, sort_keys=True))


if __name__ == "__main__":
    main()
