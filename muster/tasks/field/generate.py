import argparse
import importlib.util
import json
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
BUILD = FAMILY / "build"
EXPLOITS = ["spoof"]

_spec = importlib.util.spec_from_file_location("field_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def part(name: str) -> str:
    return (TEMPLATE / "instructions" / f"{name}.md").read_text().strip()


def instruction(config: dict, instance: dict, seat: str) -> str:
    agents = len(instance["seats"])
    rule = config["conditions"][instance["condition"]].get("rule")
    assert agents >= 1 and (agents > 1 or rule is None)
    team = "alone" if agents == 1 else "talk" if instance["channel"] else "silent"
    size, rooms, treasure = config["room_size"], config["rooms"], config["treasure_rooms"]
    others = "1 other agent works" if agents == 2 else f"{agents - 1} other agents work"
    text = (TEMPLATE / "instructions" / "frame.md").read_text().replace("{team}", part(team))
    values = {
        "seat": seat,
        "boxes": rooms * size,
        "rooms": rooms,
        "size": size,
        "second_low": size + 1,
        "second_high": 2 * size,
        "treasure": treasure,
        "empty": rooms - treasure,
        "flags": treasure * size,
        "budget": instance["budget"],
        "agents": agents,
        "others": others[0].upper() + others[1:],
    }
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert "{" not in text.replace('{"box": N}', "").replace('{"text": "..."}', ""), text
    if rule in ("claim", "scout"):
        text += "\n" + part(rule) + "\n"
    return text


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    out = {}
    for condition in config["conditions"]:
        policies = {name: reference.render_policy(config, condition, name) for name in reference.POLICIES}
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
                    f"look for the flags in {config['rooms'] * config['room_size']} boxes."
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
    parser = argparse.ArgumentParser(description="Write the task directories of the field family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ or the tables differ")
    args = parser.parse_args(argv)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status |= reference.check(reference.family())
    return status


if __name__ == "__main__":
    sys.exit(main())
