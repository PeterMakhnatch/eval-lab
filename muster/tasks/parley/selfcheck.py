import json
from collections import Counter
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory

from muster.lab import episode, search
from muster.lab.schedule import Bursty, RoundRobin, Seeded
from muster.tasks.parley import instances, lab

SEEDS = (0, 1, 2)
WORST_TRIES = 24


def verify_oracle(result, row):
    assert result.ended == "all_done" and result.state == result.replayed, (row["condition"], row["id"])
    assert result.rewards["reward"] == 1.0 and result.rewards["ended"] == 1.0, (row["condition"], row["id"], result.rewards)
    assert result.rewards["late_added"] == float(row["late"] is not None)
    assert result.rewards["refused_for_budget"] == 0.0
    for event in result.events:
        assert event["status"] != 500
    if row["late"]:
        appends = [event for event in result.events if event["kind"] == "late.append"]
        assert len(appends) == 1
    last_batches = {}
    for index, event in enumerate(result.events):
        if event["kind"] == "batch" and event["ok"]:
            last_batches[event["seat"]] = index
    for index in last_batches.values():
        if result.events[index]["request"]["writes"]:
            reduced = [dict(event, seq=i) for i, event in enumerate(
                (event for j, event in enumerate(result.events) if j != index), 1)]
            assert lab.score(reduced, row)["reward"] < 1.0


def convergence():
    count = 0
    for condition in instances.conditions():
        for row in instances.instances(condition):
            lineup = dict.fromkeys(row["real"], "oracle")
            schedules = [RoundRobin(), Seeded(SEEDS[0]), Seeded(SEEDS[1]), Bursty(SEEDS[2], 8)]
            for schedule in schedules:
                verify_oracle(episode.run(lab, row, lineup, schedule), row)
                count += 1
            if len(lineup) > 1:
                result = search.worst(lab, row, lineup, measure="reward", tries=WORST_TRIES, seed=0)
                verify_oracle(result, row)
                count += 1
    print(f"Oracle convergence: {count} selected schedules including 100 worst searches", flush=True)


def sharer_and_quitter():
    losses, applicable, failures = Counter(), Counter(), Counter()
    for facet in instances.FACETS[1:]:
        condition = f"{facet}-team"
        for row in instances.instances(condition):
            result = episode.run(lab, row, dict.fromkeys(row["real"], "sharer"), RoundRobin())
            losses[condition] += result.rewards["reward"] < 1.0
            if not row["late"]:
                continue
            for quitter in row["real"]:
                if quitter == row["late"]["seat"]:
                    continue
                lineup = dict.fromkeys(row["real"], "oracle")
                lineup[quitter] = "quitter"
                result = episode.run(lab, row, lineup, RoundRobin())
                appends = [event["seq"] for event in result.events if event["kind"] == "late.append"]
                early = [event["seq"] for event in result.events if event["seat"] == quitter
                         and event["kind"] == "ready" and event["ok"]]
                if early and appends and early[0] < appends[0]:
                    applicable[condition] += 1
                    failures[condition] += result.rewards["ended"] == 0.0
    assert all(n >= 8 for n in losses.values()) and len(losses) == 4, losses
    assert failures == applicable and sum(applicable.values()) > 0, (applicable, failures)
    print(f"Sharer round losses: {dict(losses)}; early quitter failures: {dict(failures)}", flush=True)


def request_contract(row, seed):
    rng = Random(seed)
    with TemporaryDirectory(prefix="parley-selfcheck-") as directory:
        path = Path(directory) / "events.jsonl"
        world = lab.make_world(row, path, lambda: 1.0)
        seat = row["real"][0]
        token = row["tokens"][seat]
        def call(method, route, body=None):
            return world.handle(method, route, token, body, "10.0.0.2")
        for _ in range(100):
            malformed = rng.choice([None, True, -1, 1.5, "5", []])
            status, _ = call("POST", "/batch", {"writes": {"stock01": malformed}})
            assert status == 400
            assert world.batches[seat] == {}
        for _ in range(40):
            assert call("POST", "/msg", {"text": "x"})[0] == 200
        assert call("POST", "/msg", {"text": "x"})[0] == 429
        for _ in range(3000 - 141):
            assert call("GET", "/rules")[0] == 200
        assert call("GET", "/book")[0] == 429
        assert world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert world.state() == lab.replay(events, row)
        assert lab.score(events, row)["refused_for_budget"] == 2


def main():
    convergence()
    sharer_and_quitter()
    for seed in SEEDS:
        request_contract(instances.instances("tight-team")[seed], seed)
    print(f"Request contract: {len(SEEDS)} seeded cases", flush=True)


if __name__ == "__main__":
    main()
