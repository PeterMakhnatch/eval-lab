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
PLACEHOLDER = re.compile(r"\{[a-z_0-9]+\}")
ENVIRONMENT = "[environment]\nbuild_timeout_sec = 600.0\n"
NO_BAKE = '\n[environment.env]\nCOMPOSE_BAKE = "false"\n'
DOCKERIGNORE = "**/__pycache__\n*.pyc\n*.pyo\n.DS_Store\n"

_spec = importlib.util.spec_from_file_location("orderladder_v1_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def _part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def _fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def instruction(instance: dict, seat: str) -> str:
    assert seat == "p1"
    handoff = instance["handoff"]
    listed = handoff["listed_steps"]
    values = {
        "seat": seat,
        "ladder_id": instance["ladder_id"],
        "domain": instance["domain"],
        "step_1": listed[0],
        "step_2": listed[1],
        "step_3": listed[2],
        "hint_token": handoff["hint_token"],
    }
    endpoints = _fill(_part("endpoints"), values)
    text = _part("frame").replace("{endpoints}", endpoints)
    rendered = _fill(text, values) + "\n"
    assert "tests/" not in rendered and all(c not in rendered for c in reference.CONDITIONS)
    return rendered


def _task_toml(text: str) -> str:
    assert text.count(ENVIRONMENT) == 1 and "[environment.env]" not in text
    return text.replace(ENVIRONMENT, ENVIRONMENT + NO_BAKE)


def _description(name: str, condition: str, instance: dict) -> str:
    return (
        f"{name}, condition {condition}: 1 operator seat p1 executes 3-stage dependency ladder "
        f"{instance['ladder_id']} ({instance['domain']}) in causal DAG order while checking live "
        f"/stage, /prereq, and /msgs for peer holds and mid-flight prerequisite revocations."
    )


def purge_bytecode(root: Path) -> None:
    if not root.exists():
        return
    for cache_dir in list(root.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pattern in ("*.pyc", "*.pyo"):
        for bc_file in list(root.rglob(pattern)):
            bc_file.unlink(missing_ok=True)


def check_no_bytecode(out: Path = BUILD) -> int:
    if not out.exists():
        return 0
    leaked = [p for p in out.rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")]
    for item in leaked[:10]:
        print(f"bytecode leak: {item}", file=sys.stderr)
    return 1 if leaked else 0


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    policies = {
        name: (TEMPLATE / "policies" / f"{name}.sh").read_text(encoding="utf-8")
        for name in reference.POLICIES
    }
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in reference.CONDITIONS:
        for instance in reference.instances(config, condition):
            seats = instance["seats"]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            oracle_reward = round(reference.simulate(instance, seat_tokens, "oracle")["reward"], 6)
            assert oracle_reward == 1.0
            files = build.task_files(
                name=f"muster/{name}",
                description=_description(name, condition, instance),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: instruction(instance, seat) for seat in seats},
                world={
                    "world.py": world_src,
                    "instance.json": json.dumps(instance, indent=2) + "\n",
                },
                verify=verify_src,
                policies=policies,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "oracle_reward": oracle_reward,
                    "exploits": list(config["exploits"]),
                },
                internet=False,
            )
            files["task.toml"] = _task_toml(files["task.toml"])
            files["environment/.dockerignore"] = DOCKERIGNORE
            files["environment/seat/.dockerignore"] = DOCKERIGNORE
            out[f"{condition}/{name}"] = files
    assert len(out) == 50
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of orderladder_v1.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        purge_bytecode(FAMILY)
        if args.clean and out.exists():
            shutil.rmtree(out)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()), check_no_bytecode(out))
    else:
        purge_bytecode(FAMILY)
        status = max(status, check_no_bytecode(out))
    return status


if __name__ == "__main__":
    sys.exit(main())
