import subprocess
import sys
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from muster.lab import episode, load  # noqa: E402
from muster.lab.schedule import Bursty, RoundRobin, Seeded  # noqa: E402
from muster.tasks.dossier import instances, lab, zoo  # noqa: E402
from muster.tasks.dossier.template import verify  # noqa: E402

CASES = 2000
SEEDS = tuple(range(12))
BURSTS = (2, 4, 6, 8, 12, 16)


def random_report(draw, zones, keys, summary):
    key = draw.choice(keys)
    statuses = ("closed",) if key in zones else (
        *instances.ORDINARY, "withdrawn", *(("disputed",) if summary else ()))
    return instances.entry(key, draw.choice(statuses), draw.randint(1, 4))


def protocol_equality():
    world = load.module(lab.ROOT / "template" / "world.py", "dossier_world",
                        siblings=(lab.KIT,))
    for seed in range(CASES):
        draw = Random(seed)
        zones, _, _ = instances.layout(draw)
        site_of = verify.site_zones({"zones": zones})
        keys = [*site_of, *zones]
        watch = draw.sample(list(site_of), 9)
        own = [random_report(draw, zones, keys, False) for _ in range(draw.randrange(12))]
        own = list({(r["key"], r["at"], r["status"]): r for r in own}.values())
        stored = [random_report(draw, zones, keys, True) for _ in range(draw.randrange(12))]
        lines = {r["key"]: {"status": r["status"], "at": r["at"], "by": "p2"} for r in stored}
        assert world.next_put(own, lines, watch, zones) == zoo.next_put(own, lines, watch, zones), seed
        for site in watch:
            reports = own + zoo.entries(lines)
            assert verify.status_of(site, site_of[site], reports) == zoo.status_of(
                site, site_of[site], reports), seed
    print(f"Protocol equality: {CASES} seeded cases", flush=True)


def score_shape(result, row):
    scores = result.rewards
    readers = sum(len(row["asks"][seat]) for seat in row["real"])
    writers = len(row["writer_focal"]) if row["arm"] == "probe" else 0
    assert scores["reader_asks"] == readers
    assert scores["writer_asks"] == writers
    assert ("reader_right" in scores) == (readers > 0)
    assert ("writer_right" in scores) == (writers > 0)
    questions = [(seat, site) for seat, asks in row["asks"].items() for site in asks]
    right = [(seat, site) for seat, site in questions
             if result.replayed["answers"][seat].get(site) == row["truth"][site]]
    focal_right = sum(seat in row["real"] or site in row["writer_focal"]
                      for seat, site in right)
    assert scores["reward"] == len(right) / len(questions)
    assert scores["focal"] == focal_right / (readers + writers)
    missing = instances.reported_watch_keys(row) - result.replayed["register"].keys()
    assert scores["reported_watch_keys_missing"] == (0 if row["arm"] == "solo" else len(missing))
    if row["arm"] == "reader":
        assert scores["focal"] == scores["reader_right"]


def policy_bounds(result, row):
    for seat, policy in result.lineup.items():
        requests = [e["kind"] for e in result.events if e["seat"] == seat
                    and not e["response"].get("played")]
        assert requests.count("ready") <= zoo.MAX_POLLS
        if policy in ("oracle", "own_first", "cleaner"):
            initial = len(row["feeds"][seat]) if policy == "cleaner" else 0
            assert requests.count("put") <= initial + zoo.MAX_REPAIR


def invariants(result, row):
    assert result.ended == "all_done"
    assert result.state == result.replayed
    assert result.rewards["reward"] == result.rewards["register_sufficient"] == 1.0
    assert result.rewards["forced_close"] == 0
    score_shape(result, row)
    policy_bounds(result, row)
    for event in result.events:
        seat, kind, reply = event["seat"], event["kind"], event["response"]
        if seat not in result.lineup or not event["ok"]:
            continue
        if kind == "feed":
            assert reply == {"reports": row["feeds"][seat]}
        elif kind == "asks":
            assert reply["asks"] == ([] if reply["phase"] == "writing" else row["asks"][seat])
        elif kind == "register":
            assert set(reply) == {"lines", "free", "version", "phase"}
            for key, line in reply["lines"].items():
                assert set(line) == {"at", "status", "by"}
                own = row["feeds"][line["by"]]
                known = {"key": key, "status": line["status"], "at": line["at"]}
                assert known in own or (known["status"] == "disputed" and any(
                    r["key"] == key and r["at"] == line["at"] for r in own))


def convergence():
    total = idle_total = 0
    for condition in lab.conditions():
        for index, row in enumerate(lab.instances(condition), 1):
            assert row["id"] == f"i{index:02}" and row["condition"] == condition
            idle = episode.run(lab, row, dict.fromkeys(lab.seats(row), "idle"), RoundRobin())
            score_shape(idle, row)
            assert idle.rewards["focal"] == 0.0
            idle_total += 1
            schedules = [RoundRobin()]
            if condition.endswith("-team"):
                schedules += [Seeded(seed) for seed in SEEDS]
                schedules += [Bursty(seed, run) for seed, run in enumerate(BURSTS)]
            for schedule in schedules:
                result = episode.run(lab, row, dict.fromkeys(lab.seats(row), "oracle"), schedule)
                invariants(result, row)
                total += 1
            if condition in ("scarce-probe", "mix-probe", "dispute-probe"):
                policies = ("ascending", "feed_order")
                if condition != "dispute-probe":
                    policies += ("reverse", "pooler")
                for policy in policies:
                    result = episode.run(lab, row, {"p1": policy}, RoundRobin())
                    score_shape(result, row)
                    assert result.rewards["reward"] < 1.0, (condition, row["id"], policy)
                if condition != "dispute-probe":
                    cleaner = episode.run(lab, row, {"p1": "cleaner"}, RoundRobin())
                    invariants(cleaner, row)
                    assert cleaner.rewards["used_off_watch"] == 1.0
    print(f"Generator assertions and convergence: {total} episodes", flush=True)
    print(f"Idle focal and score shape: {idle_total} episodes", flush=True)


def raw_attempts(seat, order):
    rules = yield from zoo.get("/rules")
    own = (yield from zoo.get("/feed"))["reports"]
    left = rules["budget_left"]["put"]
    before = yield from zoo.get("/register")
    for report in instances.writing_orders(own)[order]:
        status, reply = yield "POST", "/put", report
        after = yield from zoo.get("/register")
        if status == 200:
            left -= 1
            assert reply["budget_left"] == left
        else:
            assert status == 409 and reply["error"] == "register full"
            assert after == before
        assert (yield from zoo.get("/rules"))["budget_left"]["put"] == left
        before = after
    assert before["free"] == 0
    asks = yield from zoo.get("/asks")
    assert asks == {"phase": "writing", "asks": []}
    status, _ = yield "POST", "/answer", {"site": rules["watch"][0], "status": "unknown"}
    assert status == 409


def capacity_and_refusals():
    from functools import partial

    total = 0
    for condition in ("scarce-probe", "mix-probe"):
        for row in lab.instances(condition):
            for order in range(4):
                result = episode.run(lab, row, {"p1": "raw"}, RoundRobin(),
                                     zoo={"raw": partial(raw_attempts, order=order)})
                assert result.ended == "all_done" and result.state == result.replayed
                assert result.rewards["forced_close"] == 1.0
                assert result.rewards["reader_right"] == 0.0
                assert result.rewards["writer_right"] < 1.0
                refused = [e for e in result.events if e["seat"] == "p1"
                           and e["kind"] == "put" and e["status"] == 409]
                assert any(e["request"]["key"] in instances.reported_watch_keys(row) for e in refused)
                total += 1
    print(f"Capacity, refused budgets and early stop: {total} episodes", flush=True)


def waiting():
    row = instances.instance("mix", "team", 0)
    zoo_names = [name for name in zoo.POLICIES if name not in ("idle", "spam", "fuzz")]
    for policy in zoo_names:
        result = episode.run(lab, row, {"p1": policy, "p2": "idle", "p3": "idle"},
                             RoundRobin())
        policy_bounds(result, row)
        assert result.ended == "all_done" and result.state == result.replayed
        assert result.rewards["ready_calls"] == zoo.MAX_POLLS
        assert result.rewards["forced_close"] == 1
    print(f"Waiting bounds: {len(zoo_names)} mixed-lineup episodes", flush=True)


def main():
    protocol_equality()
    convergence()
    capacity_and_refusals()
    waiting()
    return subprocess.run([sys.executable, "-m", "muster", "card", "dossier", "--jobs", "0"],
                          cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
