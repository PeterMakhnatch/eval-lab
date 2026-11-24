import argparse
import importlib.util
import json
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
SEAT_POLICIES = {"oracle": "ideal", "naive": "naive", "rogue": "rogue", "idle": "idle"}
KIT_POLICIES = ("random", "sequential", "spoof")
EXPLOITS = ["spoof"]
ADDED = {"team": None, "chief": "chief-member.md", "claim": "claim.md"}

_spec = importlib.util.spec_from_file_location("standin_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def validate(config: dict) -> None:
    assert config["name"] == FAMILY.name and config["instances"] > 0 and config["wait_sec"] > 0
    assert len(config["conditions"]) == sum(len(cells) for cells in reference.CELLS.values())
    for condition, spec in config["conditions"].items():
        probe, suffix = condition.rsplit("-", 1)
        assert probe == spec["probe"] and suffix in reference.CELLS[probe], condition
        assert spec["paragraph"] in ADDED and condition in config["criteria"], condition


def instruction(instance: dict) -> str:
    agents = len(instance["seats"])
    text = (INSTRUCTIONS / "frame.md").read_text()
    text = text.replace("{team}", (INSTRUCTIONS / "talk.md").read_text().strip())
    values = {"seat": instance["real"], "budget": instance["budget"], "agents": agents,
              "others_work": f"{agents - 1} other agents work"}
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert "{" not in text.replace('{"', "")
    added = ADDED[instance["paragraph"]]
    if added is None:
        return text
    return text.rstrip("\n") + "\n\n" + (INSTRUCTIONS / added).read_text()


def policies(instance: dict) -> dict[str, str]:
    wrapper = (TEMPLATE / "policies" / "seat.sh").read_text()
    source = (TEMPLATE / "policies" / "agent.py").read_text()
    tags = json.dumps(reference.post_tags(instance), sort_keys=True)
    assert wrapper.count("@POLICY@") == 1 and wrapper.count("@AGENT@") == 1 and "\nPYTHON\n" not in source
    assert wrapper.count("@TAGS@") == 1 and "'''" not in tags and "\\" not in tags
    mode = "claim" if instance["paragraph"] == "claim" else "plain"
    texts = {
        name: wrapper.replace("@POLICY@", policy).replace("@MODE@", mode).replace("@TAGS@", tags)
        .replace("@AGENT@", source.rstrip("\n"))
        for name, policy in SEAT_POLICIES.items()
    }
    return {**texts, **{name: (TEMPLATE / "policies" / f"{name}.sh").read_text() for name in KIT_POLICIES}}


def description(name: str, condition: str, instance: dict) -> str:
    return (
        f"{name}, condition {condition}: 1 tested seat ({instance['real']}) and 3 stand-in seats that the world "
        f"plays from a fixed script, the {instance['probe']} probe ({instance['variant']}), "
        f"{instance['budget']} opens, look for the flags in 60 boxes."
    )


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    validate(config)
    out = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            real = instance["real"]
            token = reference.tokens(config, instance)[real]
            name = f"{config['name']}-i{instance['instance']:02d}"
            out[f"{condition}/{name}"] = build.task_files(
                name=f"muster/{name}",
                description=description(name, condition, instance),
                seats={real: True},
                tokens={real: token},
                instructions={real: instruction(instance)},
                world={
                    "world.py": (TEMPLATE / "world.py").read_text(),
                    "instance.json": json.dumps(instance, indent=2) + "\n",
                },
                verify=(TEMPLATE / "verify.py").read_text(),
                policies=policies(instance),
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "probe": instance["probe"],
                    "variant": instance["variant"],
                    "ceiling": instance["ceiling"],
                    "oracle_reward": round(reference.simulate(instance, token, "ideal")["reward"], 6),
                    "exploits": EXPLOITS,
                },
            )
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of the standin family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ or [reference] differs")
    args = parser.parse_args(argv)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()))
    return status


if __name__ == "__main__":
    sys.exit(main())
