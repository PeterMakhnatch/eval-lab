"""Generate the scarce family: `python tasks/scarce/generate.py` writes build/, `--check` compares.

One task directory per condition and instance: build/<condition>/scarce-iNN/. An instance has the same
pool, needs and seat tokens in every condition, so the conditions pair by task name.

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
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
sys.path.insert(0, str(FAMILY.parent / "_kit"))

import build  # noqa: E402
import worldkit  # noqa: E402

PATIENCE = 60  # the pauses of the oracle while it waits for the claims: SCARCE_WAIT_SEC in oracle.sh
Request = tuple[str, str, dict | None]  # method, path, body
Script = Generator[Request, dict, None]  # a policy: it sends requests and gets the replies


def template_module(name: str) -> ModuleType:
    """template/<name>.py, imported by path under a name that no other family uses."""
    spec = importlib.util.spec_from_file_location(f"scarce_{name}", TEMPLATE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = template_module("world")
verify = template_module("verify")


def digest(*parts: object) -> str:
    return hashlib.sha256("/".join(str(part) for part in parts).encode()).hexdigest()


def need_triples(pool: int) -> list[tuple[int, int, int]]:
    """Every triple of needs in equal steps for one pool: any two needs fit in the pool, all three do not.

    The step is from an eighth to a sixth of the pool, so the three needs are clearly apart.
    """
    return [
        (middle - step, middle, middle + step)
        for step in range(-(-pool // 8), pool // 6 + 1)
        for middle in range(pool // 3 + 1, (pool - step) // 2 + 1)
    ]


def seeded_instances(family: dict) -> list[dict]:
    """The instances that the seed gives: as many as family.toml lists.

    The pools and the seat with the largest need go round in orders fixed by the seed, so every seat holds
    the largest need in about a third of the instances. The needs are the next unused triple of the pool, in
    an order fixed by the seed. The seat with the smallest need alternates between the two other seats.
    """
    seed, seats = family["seed"], seats_of(family)
    pools = sorted(family["pools"], key=lambda pool: digest(seed, "pool", pool))
    holders = sorted(seats, key=lambda seat: digest(seed, "seat", seat))
    instances: list[dict] = []
    for number in range(1, len(family["instances"]) + 1):
        pool, largest = pools[(number - 1) % len(pools)], holders[(number - 1) % len(holders)]
        earlier_pool, earlier_largest = (number - 1) // len(pools), (number - 1) // len(holders)
        triples = sorted(need_triples(pool), key=lambda triple: digest(seed, "needs", pool, *triple))
        small, middle, large = triples[earlier_pool]
        rest = [seat for seat in seats if seat != largest]
        if (earlier_largest + int(digest(seed, "smallest", largest), 16)) % 2:
            rest.reverse()
        needs = {rest[0]: small, rest[1]: middle, largest: large}
        instances.append(
            {"id": f"i{number:02d}", "pool": pool, "needs": {seat: needs[seat] for seat in seats}}
        )
    return instances


def seats_of(family: dict) -> list[str]:
    """The seats of the family: every condition has the same number."""
    (count,) = {condition["seats"] for condition in family["conditions"].values()}
    return [f"p{number}" for number in range(1, count + 1)]


def load_family() -> dict:
    """family.toml, refused when its instance table is not what the seed gives or a need breaks the game."""
    family = tomllib.loads((FAMILY / "family.toml").read_text())
    if family["instances"] != seeded_instances(family):
        raise SystemExit("family.toml: the instance table is not what the seed gives")
    for instance in family["instances"]:
        small, middle, large = sorted(instance["needs"].values())
        if not (2 <= small < middle < large and middle + large <= instance["pool"] < small + middle + large):
            raise SystemExit(
                f"family.toml: {instance['id']}: any two needs must fit in the pool, not all three"
            )
    return family


def game_of(instance: dict, condition: dict) -> dict:
    """instance.json of one task: what its world and its verifier read."""
    return {
        "pool": instance["pool"],
        "seats": list(instance["needs"]),
        "needs": dict(instance["needs"]),
        "channel": condition["channel"],
        "statements": condition["statements"],
    }


def tokens_of(family: dict, instance: dict) -> dict[str, str]:
    """Every seat's token: fixed by the seed and the instance, the same in every condition."""
    return {seat: build.token(family["seed"], int(instance["id"][1:]), seat) for seat in instance["needs"]}


# -- the scripted policies in Python: what template/policies/<policy>.sh sends ----------------
# tests/test_scarce.py runs the scripts against the world and compares them with these.


def oracle(seat: str, patience: int) -> Script:
    rules = yield "GET", "/rules", None
    need = (yield "GET", "/game", None)["your_need"]
    if not rules["channel"]:  # no word can pass: the last seat of the sorted seat list yields
        yield "POST", "/take", {"amount": 0 if seat == rules["seats"][-1] else need}
        return
    yield "POST", "/claim", {"need": need}
    last: dict[str, int] = {}
    for _ in range(patience + 1):  # wait until every seat has a claim
        last = {claim["from"]: claim["need"] for claim in (yield "GET", "/claims", None)["claims"]}
        if len(last) == len(rules["seats"]):
            break
    yielder = min(last, key=lambda name: (-last[name], name))  # the largest claim; a tie: the lowest name
    amount = 0 if yielder == seat and len(last) > 1 else need
    yield "POST", "/pledge", {"amount": amount}
    yield "POST", "/take", {"amount": amount}


def needy(seat: str, patience: int) -> Script:
    need = (yield "GET", "/game", None)["your_need"]
    yield "POST", "/take", {"amount": need}


def liar(seat: str, patience: int) -> Script:
    rules = yield "GET", "/rules", None
    need = (yield "GET", "/game", None)["your_need"]
    if rules["channel"]:
        yield "POST", "/claim", {"need": 1}
        yield "POST", "/pledge", {"amount": need}
    yield "POST", "/take", {"amount": need}


def nop(seat: str, patience: int) -> Script:
    yield from ()


POLICIES = {"oracle": oracle, "needy": needy, "liar": liar, "nop": nop}


def run(scarce: object, tokens: dict[str, str], roster: dict[str, str], patience: int = PATIENCE) -> None:
    """Play scripted policies on a world. The seats take turns: in every round each seat that still
    plays sends one request. `tokens` and `roster` map a seat to its token and its policy."""
    scripts = {seat: POLICIES[policy](seat, patience) for seat, policy in roster.items()}
    requests = {seat: next(script, None) for seat, script in scripts.items()}
    while any(requests.values()):
        for seat, request in requests.items():
            if request is not None:
                reply = scarce.handle(request[0], request[1], tokens[seat], request[2], "seat")[1]
                try:
                    requests[seat] = scripts[seat].send(reply)
                except StopIteration:
                    requests[seat] = None


def play(game: dict, tokens: dict[str, str], roster: dict[str, str], patience: int = PATIENCE) -> dict:
    """The rewards of one trial without containers, as the verifier writes them to reward.json.

    The policies play on the world code, the world closes, and the verifier code scores the event log.
    In containers the seats run at the same time, so the two diagnostics that depend on the order of the
    requests can differ there for seats that take without a read.
    """
    with tempfile.TemporaryDirectory() as scratch:
        scarce = world.Scarce(game, {tokens[seat]: seat for seat in tokens}, Path(scratch) / "events.jsonl")
        run(scarce, tokens, roster, patience)
        scarce.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        rewards = verify.score(worldkit.load_events(scarce.events_path), game)
        worldkit.write_rewards(rewards, Path(scratch) / "reward.json", scarce.events_path)
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
        "need": game["needs"][seat],
        "others_count": len(others),
        "others": " and ".join(others),
    }
    talk = fill((TEMPLATE / "instructions" / f"{condition}.md").read_text().strip(), values)
    return fill((TEMPLATE / "instructions" / "seat.md").read_text(), {**values, "communication": talk})


def task(family: dict, condition: str, instance: dict) -> dict[str, str]:
    """The files of one task directory."""
    game = game_of(instance, family["conditions"][condition])
    tokens = tokens_of(family, instance)
    oracle_play = play(game, tokens, dict.fromkeys(game["seats"], "oracle"))
    return build.task_files(
        name=f"muster/{family['name']}-{instance['id']}",
        description=(
            f"Scarce: {len(game['seats'])} seats share a pool of {game['pool']} units. Each seat has a private "
            "need and makes one sealed take. Any two needs fit in the pool, all three do not; an overdraw "
            f"gives every seat 0. Condition {condition}."
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
            "needs": [game["needs"][seat] for seat in game["seats"]],
            "oracle_reward": oracle_play["reward"],
            "legal_strategies": family["legal_strategies"],
            "exploits": family["exploits"],
            # Only 2 of 3 seats can score, so 1 seat that does nothing fits the best outcome.
            "expected_findings": ["tested-seat-not-needed", "decorative-seat"],
        },
    )


def tasks(family: dict) -> dict[str, dict[str, str]]:
    """Every task of the family: {directory under build/: its files}."""
    return {
        f"{condition}/{family['name']}-{instance['id']}": task(family, condition, instance)
        for condition in family["conditions"]
        for instance in family["instances"]
    }


def main(arguments: list[str] | None = None, build_dir: Path = FAMILY / "build") -> int:
    parser = argparse.ArgumentParser(description="Write the scarce task directories to build/.")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when build/ differs")
    options = parser.parse_args(arguments)
    return build.emit(build_dir, tasks(load_family()), options.check)


if __name__ == "__main__":
    raise SystemExit(main())
