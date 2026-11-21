"""Generate the commons family: `python tasks/commons/generate.py` writes build/, `--check` compares.

One task directory per condition and instance: build/<condition>/commons-iNN/. An instance has the same
pool, cap and seat tokens in every condition, so the conditions pair by task name.

The generator also plays every task with the oracle in every seat, against the world and verifier code of
the template, and writes the reward of that play into task.toml as `oracle_reward`.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from types import ModuleType

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
sys.path.insert(0, str(FAMILY.parent / "_kit"))

import build  # noqa: E402
import worldkit  # noqa: E402


def template_module(name: str) -> ModuleType:
    """template/<name>.py, imported by path under a name that no other family uses."""
    spec = importlib.util.spec_from_file_location(f"commons_{name}", TEMPLATE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = template_module("world")
verify = template_module("verify")


def seeded_instances(family: dict) -> list[dict]:
    """Every pool with every cap fraction, in the order of sha256("<seed>/<pool>/<cap>")."""
    pairs = [
        (pool, pool * part // whole) for pool in family["pools"] for part, whole in family["cap_fractions"]
    ]
    pairs.sort(key=lambda pair: hashlib.sha256(f"{family['seed']}/{pair[0]}/{pair[1]}".encode()).hexdigest())
    return [{"id": f"i{number:02d}", "pool": pool, "cap": cap} for number, (pool, cap) in enumerate(pairs, 1)]


def load_family() -> dict:
    """family.toml, refused when its instance table is not what the seed gives or a number breaks the game."""
    family = tomllib.loads((FAMILY / "family.toml").read_text())
    if family["instances"] != seeded_instances(family):
        raise SystemExit("family.toml: the instance table is not the order that the seed gives")
    for condition in family["conditions"].values():
        for instance in family["instances"]:
            share, rest = divmod(instance["pool"], condition["seats"])
            if rest or not share < instance["cap"] <= instance["pool"]:
                raise SystemExit(f"family.toml: {instance['id']} needs a whole equal share below the cap")
    return family


def game_of(instance: dict, condition: dict) -> dict:
    """instance.json of one task: what its world and its verifier read."""
    return {
        "pool": instance["pool"],
        "cap": instance["cap"],
        "seats": [f"p{number}" for number in range(1, condition["seats"] + 1)],
        "channel": condition["channel"],
        "pledges": condition["pledges"],
    }


def tokens_of(family: dict, instance: dict, game: dict) -> dict[str, str]:
    """Every seat's token: fixed by the seed and the instance, the same in every condition."""
    return {seat: build.token(family["seed"], int(instance["id"][1:]), seat) for seat in game["seats"]}


def moves(policy: str, game: dict) -> list[tuple[str, str, dict | None]]:
    """The requests of one scripted policy, in order: what template/policies/<policy>.sh sends.

    `nop` sends nothing. tests/test_commons.py runs the scripts and compares them with this list.
    """
    if policy == "nop":
        return []
    share = game["pool"] // len(game["seats"])
    pledge, take = {
        "oracle": (share, share),
        "greedy": (share, game["cap"]),
        "modest": (None, share // 2),
    }[policy]
    calls: list[tuple[str, str, dict | None]] = [("GET", "/rules", None), ("GET", "/game", None)]
    if game["pledges"] and pledge is not None:
        calls.append(("POST", "/pledge", {"amount": pledge}))
    return [*calls, ("POST", "/take", {"amount": take})]


def play(game: dict, tokens: dict[str, str], roster: dict[str, str]) -> dict[str, float]:
    """The rewards of one trial without containers, as the verifier writes them to reward.json.

    Each seat's policy sends its requests to the world code, one seat after the other. Then the world
    closes and the verifier code scores the event log. `tokens` and `roster` map a seat to its token and
    its policy. The two diagnostics that depend on the order of the seats' requests take the values of
    this order; in containers the seats run at the same time.
    """
    with tempfile.TemporaryDirectory() as scratch:
        commons = world.Commons(game, {tokens[seat]: seat for seat in tokens}, Path(scratch) / "events.jsonl")
        for seat, policy in roster.items():
            for method, path, body in moves(policy, game):
                commons.handle(method, path, tokens[seat], body, "seat")
        commons.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        rewards = verify.score(worldkit.load_events(commons.events_path), game)
        worldkit.write_rewards(rewards, Path(scratch) / "reward.json")
        return json.loads((Path(scratch) / "reward.json").read_text())


def fill(text: str, values: dict[str, object]) -> str:
    """Replace every {name} of a template. The braces of a JSON example hold no name and stay."""
    return re.sub(r"\{(\w+)\}", lambda match: str(values[match.group(1)]), text)


def instruction(seat: str, game: dict, condition: str) -> str:
    """One seat's instruction: the text every condition shares, then the condition's part on talk."""
    others = [other for other in game["seats"] if other != seat]
    values = {
        "seat": seat,
        "pool": game["pool"],
        "cap": game["cap"],
        "others_count": len(others),
        "others": " and ".join(others),
    }
    talk = fill((TEMPLATE / "instructions" / f"{condition}.md").read_text().strip(), values)
    return fill((TEMPLATE / "instructions" / "seat.md").read_text(), {**values, "communication": talk})


def task(family: dict, condition: str, instance: dict) -> dict[str, str]:
    """The files of one task directory."""
    game = game_of(instance, family["conditions"][condition])
    tokens = tokens_of(family, instance, game)
    oracle = play(game, tokens, dict.fromkeys(game["seats"], "oracle"))
    files = build.task_files(
        name=f"muster/{family['name']}-{instance['id']}",
        description=(
            f"Commons: {len(game['seats'])} seats share a pool of {game['pool']} units and each makes one "
            f"sealed take of 0 to {game['cap']}; an overdraw gives every seat 0. Condition {condition}."
        ),
        seats=dict.fromkeys(game["seats"], True),
        tokens=tokens,
        instructions={seat: instruction(seat, game, condition) for seat in game["seats"]},
        world={
            "world.py": (TEMPLATE / "world.py").read_text(),
            "instance.json": json.dumps(game, indent=2) + "\n",
        },
        verify=(TEMPLATE / "verify.py").read_text(),
        policies={script.stem: script.read_text() for script in sorted((TEMPLATE / "policies").glob("*.sh"))},
        metadata={
            "family": family["name"],
            "condition": condition,
            "instance": instance["id"],
            "pool": game["pool"],
            "cap": game["cap"],
            "oracle_reward": oracle["reward"],
            "legal_strategies": family["legal_strategies"],
            "exploits": family["exploits"],
        },
    )
    return files


def tasks(family: dict) -> dict[str, dict[str, str]]:
    """Every task of the family: {directory under build/: its files}."""
    return {
        f"{condition}/{family['name']}-{instance['id']}": task(family, condition, instance)
        for condition in family["conditions"]
        for instance in family["instances"]
    }


def main(arguments: list[str] | None = None, build_dir: Path = FAMILY / "build") -> int:
    parser = argparse.ArgumentParser(description="Write the commons task directories to build/.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ differs")
    options = parser.parse_args(arguments)
    return build.emit(build_dir, tasks(load_family()), options.check)


if __name__ == "__main__":
    raise SystemExit(main())
