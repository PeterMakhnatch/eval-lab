"""Generator of the ledger family (docs/DESIGN.md, section 5).

python tasks/ledger/generate.py           writes tasks/ledger/build/<condition>/ledger-iNN/
python tasks/ledger/generate.py --check   exits 1 when build/ differs from what the generator writes

Every value comes from family.toml and template/. The ledgers are drawn with SHA-256 and not with the
`random` module, so the same family.toml gives the same bytes on every Python version.

For each instance the generator plays the oracle policy against the real world class, in every seat
start order, and grades the event log with the real verifier. The result must be the reference score of
the condition. A draw of ledgers that gives another value is refused, and the next draw is tried.
"""

import argparse
import functools
import hashlib
import importlib.util
import itertools
import json
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template"
BUILD = HERE / "build"
sys.path.insert(0, str(HERE.parent / "_kit"))

import build  # noqa: E402  the shared kit, tasks/_kit
import worldkit  # noqa: E402


def load(name: str, path: Path):
    """Import one template file by its path, as the task directory will hold it."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAMILY = tomllib.loads((HERE / "family.toml").read_text())
world = load("ledger_world", TEMPLATE / "world.py")
verify = load("ledger_verify", TEMPLATE / "verify.py")
MAX_DRAWS = 100


# -- the instance ------------------------------------------------------------------------------


def pick(*key: object, below: int) -> int:
    """A fixed number in range(below) for this key and the family seed."""
    text = "/".join(str(part) for part in (FAMILY["seed"], *key))
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") % below


def draw_ledgers(instance: int, draw: int) -> dict[str, list[dict]]:
    """The ledgers of one draw: per warehouse, rows of {"item", "qty"} with qty in 1..99."""
    items = FAMILY["items"]
    return {
        warehouse: [
            {
                "item": items[pick(instance, draw, warehouse, row, "item", below=len(items))],
                "qty": 1 + pick(instance, draw, warehouse, row, "qty", below=99),
            }
            for row in range(FAMILY["rows_per_ledger"])
        ]
        for warehouse in FAMILY["warehouses"]
    }


def ledgers_holding(ledgers: dict[str, list[dict]], item: str) -> list[str]:
    return [warehouse for warehouse, rows in ledgers.items() if any(row["item"] == item for row in rows)]


def tokens(instance: int, condition: str) -> dict[str, str]:
    seats = FAMILY["conditions"][condition]["seats"]
    return {seat: build.token(FAMILY["seed"], instance, seat) for seat in seats}


def world_instance(instance: int, condition: str, ledgers: dict[str, list[dict]]) -> dict:
    """The content of /world/instance.json: the only copy of the ledgers, and it stays in main."""
    spec = FAMILY["conditions"][condition]
    return {
        "family": FAMILY["name"],
        "instance": f"i{instance:02d}",
        "condition": condition,
        "items": FAMILY["items"],
        "channel": spec["channel"],
        "holds": spec["holds"],
        "ledgers": ledgers,
    }


# -- the oracle, played in Python ----------------------------------------------------------------


def oracle(call):
    """template/policies/oracle.sh, one request per step. `call(method, path, body)` gives the reply."""
    rules = call("GET", "/rules")
    yield
    ledger = call("GET", "/ledger")
    rows = ledger["rows"]
    yield
    items, me, seats = rules["items"], rules["you"], rules["seats"]
    own = {item: sum(row["qty"] for row in rows if row["item"] == item) for item in items}
    if len(seats) == 1 or not rules["channel"]:
        call("POST", "/answer", {"totals": own})
        return
    report = {"ledger_subtotals": own, "warehouses": ledger["warehouses"]}
    call("POST", "/msg", {"text": json.dumps(report)})
    yield
    if me != min(seats):
        while not call("GET", "/status")["answered"]:
            yield
        return
    others = [seat for seat in seats if seat != me]
    while True:
        reports = {}
        for message in call("GET", "/msgs")["messages"]:
            try:
                subtotals = json.loads(message["text"])["ledger_subtotals"]
            except (ValueError, KeyError, TypeError):
                continue
            if message["from"] != me and world.well_formed(subtotals, items):
                reports[message["from"]] = subtotals
        if all(seat in reports for seat in others):
            totals = {item: own[item] + sum(reports[seat][item] for seat in others) for item in items}
            call("POST", "/answer", {"totals": totals})
            return
        yield


def play(instance: int, condition: str, ledgers: dict[str, list[dict]], order: tuple[str, ...]) -> dict:
    """Every seat plays oracle, taking turns in `order`. Returns what the verifier writes."""
    document = world_instance(instance, condition, ledgers)
    seat_tokens = tokens(instance, condition)
    by_token = {token: seat for seat, token in seat_tokens.items()}
    with tempfile.TemporaryDirectory() as scratch:
        sim = world.Ledger(document, by_token, Path(scratch) / "events.jsonl")

        def caller(seat: str):
            def call(method: str, path: str, body: dict | None = None) -> dict:
                return sim.handle(method, path, seat_tokens[seat], body, "seat")[1]

            return call

        running = {seat: oracle(caller(seat)) for seat in order}
        for _ in range(50):
            for seat in [seat for seat in order if seat in running]:
                if next(running[seat], "done") == "done":
                    del running[seat]
            if not running:
                break
        else:
            raise RuntimeError(f"the oracles of {condition} i{instance:02d} do not finish in order {order}")
        sim.handle("POST", "/admin/close", None, {}, "127.0.0.1")
        events = worldkit.load_events(sim.events_path)
    return verify.score(events, document)


def oracle_rewards(instance: int, condition: str, ledgers: dict[str, list[dict]]) -> set[float]:
    """The all-oracle reward for every seat start order. A sound instance gives one value."""
    seats = FAMILY["conditions"][condition]["seats"]
    return {play(instance, condition, ledgers, order)["reward"] for order in itertools.permutations(seats)}


def problems(instance: int, ledgers: dict[str, list[dict]]) -> list[str]:
    """Why this draw of ledgers cannot be the instance. Empty when it can."""
    found = []
    for item in FAMILY["items"]:
        if len(ledgers_holding(ledgers, item)) < FAMILY["min_ledgers_per_item"]:
            found.append(f"{item} is in fewer than {FAMILY['min_ledgers_per_item']} ledgers")
    for condition, reference in FAMILY["reference"].items():
        rewards = oracle_rewards(instance, condition, ledgers)
        if rewards != {reference}:
            found.append(f"{condition}: the oracle gets {sorted(rewards)}, the reference is {reference}")
    return found


@functools.cache
def instance_ledgers(instance: int) -> tuple[int, dict[str, list[dict]]]:
    """The first sound draw of this instance: (draw number, ledgers). The same in every condition."""
    for draw in range(MAX_DRAWS):
        ledgers = draw_ledgers(instance, draw)
        if not problems(instance, ledgers):
            return draw, ledgers
    raise RuntimeError(f"no sound ledgers for instance {instance} in {MAX_DRAWS} draws")


# -- the task directories ------------------------------------------------------------------------


def listed(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def instruction(condition: str, seat: str) -> str:
    """instructions/<seat>.md from template/instructions/<condition>.md."""
    spec = FAMILY["conditions"][condition]
    warehouses = FAMILY["warehouses"]
    others = [other for other in spec["seats"] if other != seat]

    def reads(holder: str) -> str:
        held = spec["holds"][holder]
        if held == warehouses:
            return f"all {len(warehouses)} ledgers"
        return f"only ledger{'s' if len(held) > 1 else ''} {listed(held)}"

    who_reads = [f"You can read {reads(seat)}", *(f"{other} {reads(other)}" for other in others)]
    values = {
        "SEAT": seat,
        "NO": str(len(others)),
        "OTHERS": listed(others) if others else "",
        "READS": ", ".join(who_reads) + ".",
        "NW": str(len(warehouses)),
        "WAREHOUSES": listed(warehouses),
        "ROWS": str(FAMILY["rows_per_ledger"]),
        "NI": str(len(FAMILY["items"])),
        "ITEMS": ", ".join(FAMILY["items"]),
        "MIN": str(FAMILY["min_ledgers_per_item"]),
    }
    text = (TEMPLATE / "instructions" / f"{condition}.md").read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    if "@" in text:
        raise ValueError(f"instructions/{condition}.md has a placeholder the generator does not know")
    return text


def tasks() -> dict[str, dict[str, str]]:
    """Every task directory of the family: {"<condition>/ledger-iNN": {relative path: text}}."""
    policies = {name: (TEMPLATE / "policies" / f"{name}.sh").read_text() for name in FAMILY["policies"]}
    if sorted(policies) != sorted(path.stem for path in (TEMPLATE / "policies").glob("*.sh")):
        raise ValueError("family.toml and template/policies do not name the same policies")
    out = {}
    for condition, spec in FAMILY["conditions"].items():
        seats = list(spec["seats"])
        held = sorted(warehouse for seat in seats for warehouse in spec["holds"][seat])
        if held != FAMILY["warehouses"] or list(spec["holds"]) != seats:
            raise ValueError(f"{condition}: the seats must hold every warehouse exactly once")
        for instance in range(1, FAMILY["instances"] + 1):
            draw, ledgers = instance_ledgers(instance)
            label = f"i{instance:02d}"
            document = world_instance(instance, condition, ledgers)
            (oracle_reward,) = oracle_rewards(instance, condition, ledgers)  # one value for every start order
            out[f"{condition}/{FAMILY['name']}-{label}"] = build.task_files(
                name=f"muster/{FAMILY['name']}-{label}",
                description=f"ledger {label}, {condition}: pool three warehouse ledgers into one final team answer.",
                seats=dict.fromkeys(seats, True),
                tokens=tokens(instance, condition),
                instructions={seat: instruction(condition, seat) for seat in seats},
                world={
                    "world.py": (TEMPLATE / "world.py").read_text(),
                    "instance.json": json.dumps(document, indent=1) + "\n",
                },
                verify=(TEMPLATE / "verify.py").read_text(),
                policies=policies,
                metadata={
                    "family": FAMILY["name"],
                    "condition": condition,
                    "instance": label,
                    "seed": FAMILY["seed"],
                    "draw": draw,
                    "channel": spec["channel"],
                    "oracle_reward": oracle_reward,
                    "exploits": FAMILY["exploits"],
                },
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare build/ and exit 1 when it differs")
    args = parser.parse_args(argv)
    return build.emit(BUILD, tasks(), args.check)


if __name__ == "__main__":
    sys.exit(main())
