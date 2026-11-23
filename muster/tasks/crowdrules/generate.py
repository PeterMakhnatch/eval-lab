import argparse
import importlib.util
import json
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
POLICIES = ("oracle", "random", "sequential", "spoof")
EXPLOITS = ["spoof"]

_spec = importlib.util.spec_from_file_location("crowdrules_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def validate(config: dict) -> None:
    assert config["name"] == FAMILY.name and config["instances"] > 0
    for condition, spec in config["conditions"].items():
        seats = spec["seats"]
        assert seats == [f"p{n}" for n in range(1, len(seats) + 1)] and 1 < len(seats) <= 9, condition
        assert len(seats) * spec["budget"] == config["total_budget"], condition
        assert spec["rule"] in reference.RULES and condition == f"{spec['rule']}-{len(seats)}", condition
        assert spec["rule"] != "zones" or str(len(seats)) in config["zones"], condition
    reference.check_zones(config)


def others_work(others: int) -> str:
    assert others > 0
    return "1 other agent works" if others == 1 else f"{others} other agents work"


def team_text(instance: dict, seat: str) -> str:
    agents = len(instance["seats"])
    text = (INSTRUCTIONS / "frame.md").read_text()
    text = text.replace("{team}", (INSTRUCTIONS / "talk.md").read_text().strip())
    values = {"seat": seat, "budget": instance["budget"], "agents": agents, "others_work": others_work(agents - 1)}
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert "{" not in text.replace('{"', "")
    return text


def words(numbers: list[int]) -> str:
    assert numbers
    text = [str(number) for number in numbers]
    return text[0] if len(text) == 1 else f"{', '.join(text[:-1])} and {text[-1]}"


def positions(numbers: list[int]) -> str:
    return f"position {numbers[0]}" if len(numbers) == 1 else f"positions {words(numbers)}"


def zone_start(zone: dict) -> str:
    rooms = zone["scout"]
    if not rooms:
        return "open no box at first."
    first = [reference.box_at(room, 1) for room in rooms]
    if len(rooms) == 1:
        return f"open box {first[0]} (the first box of room {rooms[0]}) and post the result with POST /msg at once."
    return (
        f"open boxes {words(first)} (the first boxes of rooms {words(rooms)}) "
        "and post each result with POST /msg at once."
    )


def zone_fill(zone: dict) -> str:
    assert zone["both"] or zone["lower"] or zone["higher"]
    assert not (zone["lower"] and zone["higher"])
    side = "lower" if zone["lower"] else "higher"
    if not zone["both"]:
        return f"Then open only {positions(zone[side])} of the {side}-numbered treasure room."
    text = f"Then open only {positions(zone['both'])} of each treasure room"
    if zone[side]:
        text += f" and {positions(zone[side])} of the {side}-numbered one"
    return text + "."


def rule_text(config: dict, instance: dict, seat: str) -> str | None:
    rule, k, number = instance["rule"], len(instance["seats"]), int(seat[1:])
    if rule == "team":
        return None
    if rule == "share":
        own = ", ".join(str(number + k * step) for step in range(6))
        return (INSTRUCTIONS / "share.md").read_text().replace("{k}", str(k)).replace("{own}", own)
    if rule == "chief":
        return (INSTRUCTIONS / ("chief-lead.md" if number == 1 else "chief-member.md")).read_text()
    zone = config["zones"][str(k)][seat]
    text = (INSTRUCTIONS / "zones.md").read_text()
    return text.replace("{start}", zone_start(zone)).replace("{fill}", zone_fill(zone))


def instruction(config: dict, instance: dict, seat: str) -> str:
    text = team_text(instance, seat)
    added = rule_text(config, instance, seat)
    if added is None:
        return text
    assert "{" not in added and len(added.split()) <= 80, (instance["condition"], seat)
    return text.rstrip("\n") + "\n\n" + added


def data_lines(config: dict, instance: dict) -> str:
    data = reference.rule_data(config, instance)
    lines = []
    for node, action in sorted(data.get("plan", {}).items()):
        lines.append(f'PLAN[{node}]="{"stop" if action is None else f"{action[0]} {action[1]}"}"')
    for seat, zone in data.get("zones", {}).items():
        for part in ("scout", "both", "lower", "higher"):
            lines.append(f'ZONE[{seat}.{part}]="{" ".join(str(value) for value in zone[part])}"')
    return "\n".join(lines)


def policies(config: dict, instance: dict) -> dict[str, str]:
    texts = {name: (TEMPLATE / "policies" / f"{name}.sh").read_text() for name in POLICIES}
    oracle = texts["oracle"]
    assert oracle.count("@RULE@") == 1 and oracle.count("@DATA@") == 1
    texts["oracle"] = oracle.replace("@RULE@", instance["rule"]).replace("@DATA@", data_lines(config, instance))
    return texts


def description(name: str, condition: str, instance: dict) -> str:
    return (
        f"{name}, condition {condition}: {len(instance['seats'])} seats with a message channel and the "
        f"{instance['rule']} rule, {instance['budget']} opens per seat, look for the flags in 60 boxes."
    )


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    validate(config)
    out = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            out[f"{condition}/{name}"] = build.task_files(
                name=f"muster/{name}",
                description=description(name, condition, instance),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: instruction(config, instance, seat) for seat in seats},
                world={
                    "world.py": (TEMPLATE / "world.py").read_text(),
                    "instance.json": json.dumps(instance, indent=2) + "\n",
                },
                verify=(TEMPLATE / "verify.py").read_text(),
                policies=policies(config, instance),
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
    parser = argparse.ArgumentParser(description="Write the task directories of the crowdrules family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ or [reference] differs")
    args = parser.parse_args(argv)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()))
    return status


if __name__ == "__main__":
    sys.exit(main())
