import subprocess
import sys
from collections import Counter
from fractions import Fraction
from itertools import permutations, product
from pathlib import Path
from tempfile import TemporaryDirectory


def budget_regressions(adapter):
    for condition in ("limbo-probe", "limbo-guard", "limbo-solo", "limbo-team"):
        for source in adapter.instances(condition):
            for mode in ("lost_reply", "dropped", "late"):
                instance = {**source, "write_budget": {**source["write_budget"], "p1": 1}}
                ticket = next(t for t in instance["queues"]["p1"]
                              if instance["faults"].get(t["id"]) == mode)
                item = ticket["item"]
                with TemporaryDirectory() as directory:
                    world = adapter.make_world(instance, Path(directory) / "events.jsonl", lambda: 0.0)
                    token = instance["tokens"]["p1"]
                    body = {"ticket": ticket["id"], "item": item,
                            "qty": world.book[item]["qty"] + ticket["delta"],
                            "expect": {item: world.book[item]["version"]}}
                    stale = {**body, "expect": {item: body["expect"][item] + 1}}
                    assert world.handle("POST", "/write", token, stale, "seat")[0] == 409
                    assert world.left("p1", "write") == 1
                    assert world.handle("POST", "/write", token, body, "seat")[0] == 504
                    assert world.handle("GET", "/rules", token, None, "seat")[1]["budget_left"]["write"] == 0
                    assert world.handle("POST", "/write", token, body, "seat")[0] == 429
                    assert world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
                    assert world.left("p1", "write") == 0
                    assert world.written(ticket["id"], item) == (mode != "dropped")
    print("504 budget and close settlement: PASS (120 scenarios)")


def pair_chances(count, *, complementary):
    patterns = [pattern for pattern in product((False, True), repeat=count)
                if not complementary or sum(pattern) == 1]
    guesses = product((False, True), repeat=count)
    return Counter(Fraction(patterns.count(guess), len(patterns)) for guess in guesses)


def retry_chances(modes):
    patterns = set(permutations(modes))
    guesses = product((False, True), repeat=len(modes))
    return Counter(Fraction(sum(all(
        (retry or mode != "dropped") and (not retry or mode != "lost_reply")
        for retry, mode in zip(guess, pattern, strict=True)
    ) for pattern in patterns), len(patterns)) for guess in guesses)


def chance_regressions():
    from muster.tasks.depot.instances import FAULT_MODES, PAIR_COUNTS

    assert min(PAIR_COUNTS.values()) >= 3
    assert len(FAULT_MODES) >= 4 and FAULT_MODES.count("dropped") >= 2
    assert "lost_reply" in FAULT_MODES
    tables = {
        "v4 pairs": pair_chances(2, complementary=True),
        **{f"v4.1 {facet} pairs": pair_chances(count, complementary=False)
           for facet, count in PAIR_COUNTS.items()},
        "v4 retries": retry_chances(("lost_reply", "dropped", "late")),
        "v4.1 retries": retry_chances(FAULT_MODES),
        "v4.1 retries given late ordinal": retry_chances(
            tuple(mode for mode in FAULT_MODES if mode != "late")),
    }
    for name, table in tables.items():
        counts = ", ".join(f"{chance} ({n} guesses)" for chance, n in sorted(table.items()))
        print(f"{name}: {counts}")
        if name.startswith("v4.1"):
            assert max(table) <= Fraction(1, 6)
    print("Fixed-guess chance bounds: PASS")


def pair_extreme(instance, tight):
    tickets = {ticket["item"]: ticket for ticket in instance["tickets"] if ticket["kind"] == "draw"}
    takes = {}
    for pair in instance["pairs"]:
        own, peer = (tickets[item] for item in pair["items"])
        slack = sum(instance["items"][item] for item in pair["items"]) - pair["floor"]
        take = slack - own["take"] + 1 if tight else 3
        assert 3 <= take <= min(slack, 20)
        assert (take + own["take"] > slack) == tight
        takes[peer["id"]] = take
    return {**instance,
            "tickets": [with_take(ticket, takes) for ticket in instance["tickets"]],
            "queues": {seat: [with_take(ticket, takes) for ticket in queue]
                       for seat, queue in instance["queues"].items()}}


def with_take(ticket, takes):
    return {**ticket, "take": takes[ticket["id"]]} if ticket["id"] in takes else ticket


def extreme_regressions(adapter):
    from muster.lab import card, episode, schedule

    count = 0
    for condition in adapter.conditions():
        if not condition.startswith(("skew-", "storm-")):
            continue
        source = adapter.instances(condition)[0]
        for tight in (False, True):
            instance = pair_extreme(source, tight)
            lineup = dict.fromkeys(adapter.seats(instance), "oracle")
            if tight:
                decliner = episode.run(adapter, instance, dict.fromkeys(lineup, "decliner"),
                                       schedule.RoundRobin())
                assert decliner.rewards["focal"] <= 0.25
            for label in card.schedule_suite(len(lineup), 12, 0):
                result = episode.run(adapter, instance, lineup, schedule.parse(label))
                assert result.ended == "all_done"
                assert result.rewards["reward"] == result.rewards["clean"] == 1.0
                assert not result.rewards["refused_for_budget"]
                count += 1
    assert count == 64
    print(f"All-roomy and all-tight counterfactuals: PASS ({count} scenarios)")


def main():
    args = sys.argv[1:]
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    from muster.lab import load

    chance_regressions()
    budget_regressions(load.family("depot"))
    extreme_regressions(load.family("depot"))
    if args == ["--quick"]:
        args = ["--instances", "2", "--schedules", "2", "--worst", "2"]
    return subprocess.run([sys.executable, "-m", "muster", "card", "depot", *args],
                          cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
