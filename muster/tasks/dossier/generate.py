import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
ROOT = FAMILY.parents[2]
TEMPLATE = FAMILY / "template"
KIT = FAMILY.parent / "_kit"
BUILD = FAMILY / "build"
SHIPPED = ("oracle", "pooler", "hoarder", "idle")


def validate(config, source, adapter):
    assert config["name"] == FAMILY.name == adapter.NAME
    assert config["seed"] == source.SEED and config["instances"] == source.COUNT
    assert tuple(config["facets"]) == source.FACETS and tuple(config["arms"]) == source.ARMS
    conditions = [f"{facet}-{arm}" for facet in config["facets"] for arm in config["arms"]]
    assert conditions == source.conditions() == adapter.conditions()
    assert tuple(config["policies"]) == SHIPPED and set(SHIPPED) <= set(adapter.policies())
    assert {f"{facet}-probe": needs for facet, needs in config["needs"].items()} == (
        adapter.skill_claim()["conditions"]
    )
    assert set(config["skills"]) == {skill for needs in config["needs"].values() for skill in needs}


def instruction(instance, seat):
    folder = TEMPLATE / "instructions"
    fragments = tomllib.loads((folder / "fragments.toml").read_text())
    arm = instance["arm"]
    others = sorted(set(instance["clerks"]) - {seat})
    solo = arm == "solo"
    assert len(others) == (0 if solo else 2)
    writing = arm in ("probe", "team")
    fills = dict(fragments[arm])
    fills.update({"REGISTER": "" if solo else fragments["common"]["REGISTER"],
                  "WRITING": fragments["common"]["WRITING"] if writing else "",
                  "SEAT": seat, "OTHERS": " and ".join(others),
                  "SLOTS": str(instance["slots"]), "PUTS": str(instance["budgets"][seat]["put"])})
    text = (folder / "seat.md").read_text()
    for key, value in fills.items():
        text = text.replace(f"@{key}@", value)
    assert "@" not in text, (instance["condition"], seat)
    assert ("## Writing phase" in text) == writing
    if not writing:
        assert all(f"POST /{name}" not in text for name in ("put", "drop", "ready"))
    if solo:
        named = set(re.findall(r"\bp[0-9]+\b", text))
        assert named == {seat} and "GET /register" not in text, named
    return text


def policies(names):
    folder = TEMPLATE / "policies"
    wrapper = (folder / "seat.sh").read_text()
    zoo = (FAMILY / "zoo.py").read_text()
    driver = (folder / "driver.py").read_text()
    assert "PYTHON" not in zoo and "PYTHON" not in driver
    result = {}
    for name in names:
        script = wrapper.replace("@POLICY@", name).replace("@ZOO@", zoo).replace("@DRIVER@", driver)
        assert script.count("PYTHON") == 2
        result[name] = script
    return result


def task(instance, config, build, common_world, scripts):
    seats = instance["real"]
    number = (config["facets"].index(instance["facet"]) * len(config["arms"])
              + config["arms"].index(instance["arm"])) * config["instances"]
    number += int(instance["id"][1:]) - 1
    tokens = {seat: build.token(config["seed"], number, seat) for seat in seats}
    assert tokens == {seat: instance["tokens"][seat] for seat in seats}
    world = {**common_world, "instance.json": json.dumps(instance, sort_keys=True, indent=1) + "\n"}
    metadata = {key: instance[key] for key in ("condition", "facet", "arm")}
    metadata.update(family="dossier", instance=instance["id"], oracle_reward=1.0,
                    card="muster/tasks/dossier/card.json")
    return build.task_files(
        name=f"muster/dossier-{instance['id']}",
        description=config["description"].strip(),
        seats=dict.fromkeys(seats, True),
        tokens=tokens,
        instructions={seat: instruction(instance, seat) for seat in seats},
        world=world,
        verify=common_world["verify.py"],
        policies=scripts,
        metadata=metadata,
        agent_timeout_sec=900,
    )


def tasks():
    from muster.lab import episode, load
    from muster.lab.schedule import RoundRobin
    from muster.tasks._kit import build
    from muster.tasks.dossier import instances as source

    config = tomllib.loads((FAMILY / "family.toml").read_text())
    adapter = load.family("dossier")
    validate(config, source, adapter)
    scripts = policies(config["policies"])
    common_world = {"world.py": (TEMPLATE / "world.py").read_text(),
                    "stagekit.py": (KIT / "stagekit.py").read_text(),
                    "verify.py": (TEMPLATE / "verify.py").read_text()}
    out = {}
    for condition in source.conditions():
        instances = source.instances(condition)
        assert len(instances) == config["instances"] and instances == adapter.instances(condition)
        first = instances[0]
        lineup = dict.fromkeys(adapter.seats(first), "oracle")
        result = episode.run(adapter, first, lineup, RoundRobin())
        assert result.ended == "all_done" and result.rewards["reward"] == 1.0, (
            condition, result.rewards
        )
        for index, instance in enumerate(instances, 1):
            assert instance["id"] == f"i{index:02d}" and instance["condition"] == condition
            assert instance["real"] == adapter.seats(instance)
            key = f"{condition}/dossier-{instance['id']}"
            assert key not in out
            out[key] = task(instance, config, build, common_world, scripts)
    return out


def main(argv=None):
    from muster.tasks._kit import build

    parser = argparse.ArgumentParser(description="Write the task directories of dossier.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = tasks()
    except Exception:
        if not args.check:
            build.emit(BUILD, {}, check=False)
        raise
    return build.emit(BUILD, expected, check=args.check)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
