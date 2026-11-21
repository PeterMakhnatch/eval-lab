"""Generator of the boxes family: `python tasks/boxes/generate.py` writes build/<condition>/boxes-iNN/.

--check writes nothing and exits 1 when build/ differs from what the generator writes.

One instance is one layout: which 2 of the 6 rooms are treasure rooms. An instance has the same layout
and the same seat tokens in every condition. `oracle_reward` in task.toml is the reward of a trial in
which every seat plays the oracle; reference.py plays that trial against the real world class.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
BUILD = FAMILY / "build"
POLICIES = ("oracle", "solo", "random", "sequential", "spoof")
EXPLOITS = ["spoof"]  # the policies that try to cheat: they must gain nothing

_spec = importlib.util.spec_from_file_location("boxes_reference", FAMILY / "reference.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def instruction(config: dict, instance: dict, seat: str) -> str:
    """The seat's instruction: the frame all conditions share, and the part about the other agents.

    A condition with `salt` adds one last line with the seat's number and no word on its use.
    """
    agents = len(instance["seats"])
    part = "alone" if agents == 1 else "talk" if instance["channel"] else "silent"
    text = (TEMPLATE / "instructions" / "frame.md").read_text()
    text = text.replace("{team}", (TEMPLATE / "instructions" / f"{part}.md").read_text().strip())
    for key, value in {
        "seat": seat,
        "budget": instance["budget"],
        "left": instance["budget"] - 1,
        "agents": agents,
        "others": agents - 1,
    }.items():
        text = text.replace(f"{{{key}}}", str(value))
    if config["conditions"][instance["condition"]].get("salt"):
        text += f"Your random number is {reference.salts(config, instance)[seat]}.\n"
    return text


def tasks() -> dict[str, dict[str, str]]:
    """Every task directory of the family: {"<condition>/boxes-iNN": {relative path: text}}."""
    config = reference.family()
    policies = {name: (TEMPLATE / "policies" / f"{name}.sh").read_text() for name in POLICIES}
    out = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            team = "1 seat" if len(seats) == 1 else f"{len(seats)} seats"
            talk = "with a message channel" if instance["channel"] else "without a message channel"
            out[f"{condition}/{name}"] = build.task_files(
                name=f"muster/{name}",
                description=(
                    f"{name}, condition {condition}: {team} {talk}, {instance['budget']} opens per seat, "
                    "look for the flags in 60 boxes."
                ),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: instruction(config, instance, seat) for seat in seats},
                world={
                    "world.py": (TEMPLATE / "world.py").read_text(),
                    "instance.json": json.dumps(instance, indent=2) + "\n",
                },
                verify=(TEMPLATE / "verify.py").read_text(),
                policies=policies,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "oracle_reward": round(reference.simulate(instance, seat_tokens)["reward"], 6),
                    "exploits": EXPLOITS,
                },
            )
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of the boxes family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ differs")
    args = parser.parse_args(argv)
    return build.emit(out, tasks(), args.check)


if __name__ == "__main__":
    sys.exit(main())
