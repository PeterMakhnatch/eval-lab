import argparse
import json
import sys
import tomllib
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
ROOT = FAMILY.parents[2]
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit"
BUILD = FAMILY / "build"


def instruction(instance, seat):
    folder = TEMPLATE / "instructions"
    arm = instance["arm"]
    if arm == "solo":
        assert seat == "p1" and instance["batch_limits"][seat] == 12
        return (folder / "solo.md").read_text()
    text = (folder / "team.md").read_text()
    first = "You are clerk p1 at a depot. Clerks p2 and p3 also work on this book."
    others = " and ".join(clerk for clerk in instance["real"] if clerk != seat)
    assert first in text
    text = text.replace(first, f"You are clerk {seat} at a depot. Clerks {others} also work on this book.", 1)
    text = text.replace("at most 6 items", f"at most {instance['batch_limits'][seat]} items", 1)
    if arm == "open":
        text = text.replace("No other clerk sees your queue. The dispatcher can add a ticket to a queue at any moment of the plan phase.",
                            "The dispatcher can add a ticket to a queue at any moment of the plan phase.\n- `GET /queues`: the queue of every clerk, and the number of items that the batch of each clerk can hold.", 1)
    assert text.count("3,000 requests") == 1
    return text


def policies(names):
    folder = TEMPLATE / "policies"
    wrapper = (folder / "seat.sh").read_text()
    zoo = (FAMILY / "zoo.py").read_text()
    driver = (folder / "driver.py").read_text()
    assert "PYTHON" not in zoo and "PYTHON" not in driver
    return {name: wrapper.replace("@POLICY@", name).replace("@ZOO@", zoo).replace("@DRIVER@", driver)
            for name in names}


def task(instance, config, build, common, scripts):
    real = instance["real"]
    world = {**common, "instance.json": json.dumps(instance, sort_keys=True, indent=1) + "\n"}
    metadata = {key: instance[key] for key in ("condition", "facet", "arm")}
    metadata.update(family="parley", instance=instance["id"], oracle_reward=1.0,
                    card="muster/tasks/parley/card.json")
    return build.task_files(name=f"muster/parley-{instance['id']}", description=config["description"],
                            seats=dict.fromkeys(real, True), tokens=instance["tokens"],
                            instructions={seat: instruction(instance, seat) for seat in real},
                            world=world, verify=common["verify.py"], policies=scripts,
                            metadata=metadata, agent_timeout_sec=900)


def tasks():
    from muster.lab import episode, load
    from muster.lab.schedule import RoundRobin
    from muster.tasks._kit import build
    from muster.tasks.parley import instances as source

    config = tomllib.loads((FAMILY / "family.toml").read_text())
    adapter = load.family("parley")
    assert config["name"] == adapter.NAME == FAMILY.name
    assert config["seed"] == source.SEED and config["instances"] == source.COUNT
    assert tuple(config["facets"]) == source.FACETS and tuple(config["arms"]) == source.ARMS
    assert set(config["policies"]) == set(adapter.policies())
    scripts = policies(config["policies"])
    common = {name: (TEMPLATE / name).read_text() for name in ("world.py", "verify.py")}
    result = {}
    for condition in source.conditions():
        rows = source.instances(condition)
        assert len(rows) == source.COUNT and rows == adapter.instances(condition)
        first = rows[0]
        run = episode.run(adapter, first, dict.fromkeys(first["real"], "oracle"), RoundRobin())
        assert run.ended == "all_done" and run.rewards["reward"] == 1.0, (condition, run.rewards)
        for index, row in enumerate(rows, 1):
            assert row["id"] == f"i{index:02d}" and row["condition"] == condition
            key = f"{condition}/parley-{row['id']}"
            assert key not in result
            result[key] = task(row, config, build, common, scripts)
    assert len(result) == len(source.conditions()) * source.COUNT
    return result


def main():
    from muster.tasks._kit import build

    parser = argparse.ArgumentParser(description="Generate Parley task directories.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return build.emit(BUILD, tasks(), check=args.check)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
