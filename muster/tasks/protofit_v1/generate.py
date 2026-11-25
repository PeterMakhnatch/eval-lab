from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")

_spec = importlib.util.spec_from_file_location("protofit_v1_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build: Any = reference.build


def _part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def _fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def instruction(instance: dict[str, Any], seat: str) -> str:
    assert seat in instance["seats"]
    values: dict[str, object] = {
        "seat": seat,
        "task_id": instance["task_id"],
        "domain": instance["domain"],
        "probe_pool": instance["probe_pool"],
        "max_msg_bytes": instance["max_msg_bytes"],
    }
    endpoints = _fill(_part("endpoints"), values)
    text = _part("frame").replace("{endpoints}", endpoints)
    rendered = _fill(text, values) + "\n"
    assert "tests/" not in rendered and all(c not in rendered for c in reference.CONDITIONS)
    assert instance["regime_code"] not in rendered and instance["target_digest"] not in rendered
    return rendered


def _description(name: str, condition: str, instance: dict[str, Any]) -> str:
    seats_txt = ", ".join(instance["seats"])
    return (
        f"{name}, condition {condition}: seats [{seats_txt}] negotiate coupled protocol "
        f"contract {instance['task_id']} ({instance['domain']}) from isolated workspace shards."
    )


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    policy_scripts = {
        name: (TEMPLATE / "policies" / f"{name}.sh").read_text(encoding="utf-8")
        for name in reference.POLICY_NAMES
    }
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in reference.CONDITIONS:
        for instance in reference.instances(config, condition):
            seats = list(instance["seats"])
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{instance['instance']:02d}"
            oracle_reward = round(reference.simulate(instance, seat_tokens, "oracle")["reward"], 6)
            seat_files = {
                seat: {"constraints.json": json.dumps(instance["seat_constraints"][seat], indent=2) + "\n"}
                for seat in seats
            }
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
                policies=policy_scripts,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": instance["instance"],
                    "oracle_reward": oracle_reward,
                    "exploits": list(config["exploits"]),
                },
                seat_files=seat_files,
                internet=False,
            )
            assert (
                '[environment.env]\nCOMPOSE_BAKE = "false"\n' in files["task.toml"]
                and files["task.toml"].count("[environment.env]") == 1
            )
            out[f"{condition}/{name}"] = files
    assert len(out) == 40
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of protofit_v1.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        build.purge_bytecode(FAMILY)
        if args.clean and out.exists():
            shutil.rmtree(out)
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(status, reference.check(reference.family()), build.check_no_bytecode(out))
    else:
        build.purge_bytecode(FAMILY)
        status = max(status, build.check_no_bytecode(out))
    return status


if __name__ == "__main__":
    sys.exit(main())
