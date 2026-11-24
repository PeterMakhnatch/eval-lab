import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
ENVIRONMENT = "[environment]\nbuild_timeout_sec = 600.0\n"
NO_BAKE = '\n[environment.env]\nCOMPOSE_BAKE = "false"\n'
DOCKERIGNORE = "**/__pycache__\n*.pyc\n.DS_Store\n"

_spec = importlib.util.spec_from_file_location("crowdscale_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build
worldkit = reference.worldkit


def part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def instruction(config: dict, instance: dict, seat: str) -> str:
    assert seat == "p1"
    k = instance["k"]
    peers = instance["peers"]
    if k == 1:
        team_line = part("solo")
    else:
        team_line = fill(part("team"), {"seat": seat, "peers_count": len(peers), "last_peer": peers[-1]})
    clue_line = part("check_channels")
    raw = (INSTRUCTIONS / "frame.md").read_text(encoding="utf-8")
    return fill(
        raw,
        {
            "seat": seat,
            "k": k,
            "team_line": team_line,
            "max_rounds": config["max_rounds"],
            "num_candidates": len(config["candidates"]),
            "endowment": config["endowment"],
            "max_per_round": config["max_contribute_per_round"],
            "clue_line": clue_line,
        },
    )


def patch_task_toml(text: str) -> str:
    assert text.count(ENVIRONMENT) == 1 and "[environment.env]" not in text
    return text.replace(ENVIRONMENT, ENVIRONMENT + NO_BAKE)


def description(name: str, condition: str, instance: dict) -> str:
    k, mode = instance["k"], instance["mode"]
    return (
        f"{name}, condition {condition}: 1 model seat p1 with {k - 1} scripted peers ({mode}, k={k}), "
        f"settle collective pool target {instance['target']} and verify the valid candidate."
    )


def play(instance: dict, seat_tokens: dict[str, str], roster: dict[str, str] | str = "oracle") -> dict[str, float]:
    return reference.simulate(instance, seat_tokens, roster)


def purge_bytecode(root: Path) -> None:
    if not root.exists():
        return
    for cache_dir in list(root.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc_file in list(root.rglob("*.pyc")):
        pyc_file.unlink(missing_ok=True)


def check_no_bytecode(out: Path = BUILD) -> int:
    if not out.exists():
        return 0
    leaked = [p for p in out.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"]
    for item in leaked[:10]:
        print(f"bytecode leak: {item}", file=sys.stderr)
    return 1 if leaked else 0


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    policies = {name: reference.render_policy(name) for name in reference.POLICIES}
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            oracle_score = round(reference.simulate(instance, seat_tokens, "oracle")["reward"], 6)
            assert oracle_score == 1.0, (condition, name, oracle_score)
            files = build.task_files(
                name=f"muster/{name}",
                description=description(name, condition, instance),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: instruction(config, instance, seat) for seat in seats},
                world={
                    "world.py": world_src,
                    "instance.json": json.dumps(instance, indent=2, sort_keys=True) + "\n",
                },
                verify=verify_src,
                policies=policies,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "k": instance["k"],
                    "mode": instance["mode"],
                    "oracle_reward": oracle_score,
                    "exploits": list(config["exploits"]),
                },
            )
            files["task.toml"] = patch_task_toml(files["task.toml"])
            files["environment/.dockerignore"] = DOCKERIGNORE
            files["environment/seat/.dockerignore"] = DOCKERIGNORE
            out[f"{condition}/{name}"] = files
    assert len(out) == 40
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Build the 40 task directories of crowdscale_v1.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    purge_bytecode(FAMILY)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()), check_no_bytecode(out))
    else:
        status = max(status, check_no_bytecode(out))
    purge_bytecode(FAMILY)
    return status


if __name__ == "__main__":
    sys.exit(main())
