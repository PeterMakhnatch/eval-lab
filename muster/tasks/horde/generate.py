import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
EXPLOITS = ["spoof"]
EXAMPLE_ROOM = 2
MAX_RULE_WORDS = 320
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
ENVIRONMENT = "[environment]\nbuild_timeout_sec = 600.0\n"
NO_BAKE = '\n[environment.env]\nCOMPOSE_BAKE = "false"\n'

_spec = importlib.util.spec_from_file_location("horde_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text().strip()


def fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def words(numbers: list[int]) -> str:
    assert numbers
    text = [str(number) for number in numbers]
    return text[0] if len(text) == 1 else f"{', '.join(text[:-1])} and {text[-1]}"


def span(noun: str, nouns: str, values: list[str]) -> str:
    assert values
    if len(values) == 1:
        return f"{noun} {values[0]}"
    joiner = "and" if len(values) == 2 else "to"
    return f"{nouns} {values[0]} {joiner} {values[-1]}"


def zone_start(config: dict, zone: dict) -> str:
    rooms = zone["scout"]
    if not rooms:
        return "Open no box now: other agents scout the rooms."
    boxes = [reference.box_at(config, room, 1) for room in rooms]
    if len(rooms) == 1:
        return f"Open box {boxes[0]}, the first box of room {rooms[0]}, and post the result with POST /msg at once."
    return (
        f"Open boxes {words(boxes)}, the first boxes of rooms {words(rooms)}, "
        "and post each result with POST /msg at once."
    )


def zone_fill(config: dict, zone: dict) -> str:
    rank, positions, size = zone["rank"], zone["positions"], config["room_size"]
    assert positions == list(range(positions[0], positions[-1] + 1))
    where = span("position", "positions", [str(p) for p in positions])
    formula = span("box", "boxes", [f"{size} * (T{rank} - 1) + {p}" for p in positions])
    return f"Then open only {where} of T{rank}, that is, {formula}."


def rule_text(config: dict, instance: dict, seat: str) -> str | None:
    rule, agents, budget = instance["rule"], len(instance["seats"]), instance["budget"]
    if rule in ("solo", "team"):
        return None
    common = {
        "rooms": config["rooms"],
        "size": config["room_size"],
        "treasure": config["treasure_rooms"],
        "budget": budget,
        "agents": agents,
        "total": config["total_budget"],
        "closed": reference.flags(config) - config["treasure_rooms"],
        "seat": seat,
    }
    if rule == "chief":
        return fill(part("chief-lead" if seat == "p1" else "chief-member"), common)
    zone = reference.zones(config, agents)[seat]
    steps = "Steps 1 and 3 use" if zone["scout"] else "Step 3 uses"
    common |= {
        "start": zone_start(config, zone),
        "fill": zone_fill(config, zone),
        "uses": f"{steps} all {budget} of your opens.",
        "example_first": reference.box_at(config, EXAMPLE_ROOM, 1),
        "example_last": reference.box_at(config, EXAMPLE_ROOM, config["room_size"]),
    }
    return fill(part("zones"), common)


def instruction(config: dict, instance: dict, seat: str) -> str:
    agents = len(instance["seats"])
    assert (agents == 1) == (not instance["channel"])
    size, rooms, treasure = config["room_size"], config["rooms"], config["treasure_rooms"]
    others = "1 other agent works" if agents == 2 else f"{agents - 1} other agents work"
    text = (INSTRUCTIONS / "frame.md").read_text().replace("{team}", part("talk" if instance["channel"] else "alone"))
    values = {
        "seat": seat,
        "boxes": rooms * size,
        "rooms": rooms,
        "size": size,
        "second_low": size + 1,
        "second_high": 2 * size,
        "treasure": treasure,
        "empty": rooms - treasure,
        "flags": reference.flags(config),
        "budget": instance["budget"],
        "agents": agents,
        "others": others[0].upper() + others[1:],
    }
    text = fill(text, values)
    added = rule_text(config, instance, seat)
    if added is None:
        return text
    assert len(added.split()) <= MAX_RULE_WORDS, (instance["condition"], seat, len(added.split()))
    return text.rstrip("\n") + "\n\n" + added + "\n"


def validate(config: dict) -> None:
    reference.check_layouts(config)
    reference.check_zones(config)
    for condition, spec in config["conditions"].items():
        assert spec["agents"] * (config["total_budget"] // spec["agents"]) == config["total_budget"], condition


def task_toml(text: str) -> str:
    assert text.count(ENVIRONMENT) == 1 and "[environment.env]" not in text
    return text.replace(ENVIRONMENT, ENVIRONMENT + NO_BAKE)


def description(config: dict, name: str, condition: str, instance: dict) -> str:
    agents = len(instance["seats"])
    team = "1 seat without a message channel" if agents == 1 else f"{agents} seats with a message channel"
    return (
        f"{name}, condition {condition}: {team} and the {instance['rule']} rule, {instance['budget']} opens per "
        f"seat ({config['total_budget']} in total), look for the flags in {config['rooms'] * config['room_size']} boxes."
    )


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    validate(config)
    out = {}
    for condition in config["conditions"]:
        policies = {name: reference.render_policy(config, condition, name) for name in reference.POLICIES}
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            files = build.task_files(
                name=f"muster/{name}",
                description=description(config, name, condition, instance),
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
            files["task.toml"] = task_toml(files["task.toml"])
            out[f"{condition}/{name}"] = files
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of the horde family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ or [reference] differs")
    args = parser.parse_args(argv)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()))
    return status


if __name__ == "__main__":
    sys.exit(main())
