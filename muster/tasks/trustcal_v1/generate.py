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
POLICIES_DIR = TEMPLATE / "policies"
BUILD = FAMILY / "build"
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
ENVIRONMENT = "[environment]\nbuild_timeout_sec = 600.0\n"
NO_BAKE = '\n[environment.env]\nCOMPOSE_BAKE = "false"\n'
DOCKERIGNORE = "**/__pycache__\n*.pyc\n.DS_Store\n"

_spec = importlib.util.spec_from_file_location("trustcal_v1_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def instruction(instance: dict, seat: str) -> str:
    template = (INSTRUCTIONS / f"{seat}.md").read_text(encoding="utf-8")
    values = {
        "seat": seat,
        "peer_rounds": instance["peer_rounds"],
        "verify_coins": instance["verify_coins"],
        "accuse_budget": instance["accuse_budget"],
    }
    return fill(template, values)


def task_toml(text: str) -> str:
    assert text.count(ENVIRONMENT) == 1 and "[environment.env]" not in text
    return text.replace(ENVIRONMENT, ENVIRONMENT + NO_BAKE)


def description(name: str, condition: str, instance: dict) -> str:
    return (
        f"{name}, condition {condition}: 1 evaluated auditor seat p1 with 3 world-scripted teammates across "
        f"{instance['peer_rounds']} peer round(s) plus Round 6 final probe ({instance['verify_coins']} verify coins)."
    )


def purge_bytecode(root: Path) -> None:
    if not root.exists():
        return
    for cache_dir in list(root.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc_file in list(root.rglob("*.pyc")):
        pyc_file.unlink(missing_ok=True)


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    policies = {name: (POLICIES_DIR / f"{name}.sh").read_text(encoding="utf-8") for name in reference.POLICIES}
    world_py = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_py = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            oracle_res = reference.simulate(instance, seat_tokens, "oracle")
            assert oracle_res["reward"] == 1.0 and oracle_res["script_faithful"] == 1.0 and oracle_res["chain_ok"] == 1.0
            files = build.task_files(
                name=f"muster/{name}",
                description=description(name, condition, instance),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: instruction(instance, seat) for seat in seats},
                world={
                    "world.py": world_py,
                    "instance.json": json.dumps(instance, indent=2, sort_keys=True) + "\n",
                },
                verify=verify_py,
                policies=policies,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "oracle_reward": round(oracle_res["reward"], 6),
                    "exploits": list(config["exploits"]),
                },
                internet=False,
            )
            files["task.toml"] = task_toml(files["task.toml"])
            files["environment/.dockerignore"] = DOCKERIGNORE
            files["environment/seat/.dockerignore"] = DOCKERIGNORE
            out[f"{condition}/{name}"] = files
    assert len(out) == 50
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    purge_bytecode(FAMILY)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()))
    purge_bytecode(FAMILY)
    return status


if __name__ == "__main__":
    sys.exit(main())
