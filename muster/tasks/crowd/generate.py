import argparse
import importlib.util
import json
import sys
from pathlib import Path

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
BUILD = FAMILY / "build"
POLICIES = ("oracle", "random", "sequential", "spoof")
EXPLOITS = ["spoof"]

_spec = importlib.util.spec_from_file_location("crowd_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def validate(config: dict) -> None:
    assert config["name"] == FAMILY.name and config["instances"] > 0
    for condition, spec in config["conditions"].items():
        seats = spec["seats"]
        assert seats == [f"p{n}" for n in range(1, len(seats) + 1)] and len(seats) <= 9, condition
        assert len(seats) * spec["budget"] == config["total_budget"] or len(seats) == 1, condition
        assert spec["budget"] <= config["total_budget"], condition
        assert not spec["claim"] or spec["channel"], condition
        assert len(seats) > 1 or not spec["channel"], condition
        assert len(seats) in reference.RULES, condition


def others_work(others: int) -> str:
    assert others > 0
    return "1 other agent works" if others == 1 else f"{others} other agents work"


def instruction(instance: dict, seat: str) -> str:
    agents = len(instance["seats"])
    part = "alone" if agents == 1 else "talk" if instance["channel"] else "silent"
    text = (TEMPLATE / "instructions" / "frame.md").read_text()
    text = text.replace("{team}", (TEMPLATE / "instructions" / f"{part}.md").read_text().strip())
    values = {"seat": seat, "budget": instance["budget"], "agents": agents}
    if agents > 1:
        values["others_work"] = others_work(agents - 1)
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert "{" not in text.replace('{"', "")
    if instance["claim"]:
        text = text.rstrip("\n") + "\n\n" + (TEMPLATE / "instructions" / "claim.md").read_text()
    return text


def tacit_lines() -> str:
    lines = []
    for k, places in sorted(reference.RULES.items()):
        lines.append(f'TACIT[{k}]="{" ".join(str(number) for place in places for number in place)}"')
    return "\n".join(lines)


def policies(instance: dict) -> dict[str, str]:
    texts = {name: (TEMPLATE / "policies" / f"{name}.sh").read_text() for name in POLICIES}
    oracle = texts["oracle"]
    assert oracle.count("@CLAIM@") == 1 and oracle.count("@TACIT@") == 1
    claim = "true" if instance["claim"] else "false"
    texts["oracle"] = oracle.replace("@CLAIM@", claim).replace("@TACIT@", tacit_lines())
    return texts


def description(name: str, condition: str, instance: dict) -> str:
    seats = len(instance["seats"])
    team = "1 seat" if seats == 1 else f"{seats} seats"
    talk = "with a message channel" if instance["channel"] else "without a message channel"
    rule = " and the claim rule" if instance["claim"] else ""
    return (
        f"{name}, condition {condition}: {team} {talk}{rule}, {instance['budget']} opens per seat, "
        "look for the flags in 60 boxes."
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
                instructions={seat: instruction(instance, seat) for seat in seats},
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
                    "oracle_reward": round(reference.simulate(instance, seat_tokens)["reward"], 6),
                    "exploits": EXPLOITS,
                },
            )
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of the crowd family.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ or [reference] differs")
    args = parser.parse_args(argv)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()))
    return status


if __name__ == "__main__":
    sys.exit(main())
