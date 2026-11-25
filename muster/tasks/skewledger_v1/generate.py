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
ENVIRONMENT = "[environment]\nbuild_timeout_sec = 600.0\n"
NO_BAKE = '\n[environment.env]\nCOMPOSE_BAKE = "false"\n'
MAIN_DOCKERFILE = "FROM python:3.13-slim\n\nENV PYTHONPATH=/world PYTHONDONTWRITEBYTECODE=1\nCOPY world/ /world/\n"
TEST_SH = "#!/bin/bash\nPYTHONPATH=/world PYTHONDONTWRITEBYTECODE=1 python3 -B /tests/verify.py\n"

_spec = importlib.util.spec_from_file_location("skewledger_v1_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build = reference.build


def _fill(text: str, values: dict[str, object]) -> str:
    for key, val in values.items():
        text = text.replace(f"{{{key}}}", str(val))
    assert not PLACEHOLDER.search(text), PLACEHOLDER.findall(text)
    return text


def instruction(instance: dict[str, Any], seat: str) -> str:
    raw = (INSTRUCTIONS / f"{seat}.md").read_text(encoding="utf-8").strip()
    values: dict[str, object] = {
        "task_id": instance["task_id"],
        "domain": instance["domain"],
        "init_bal_a": instance["init_bal_a"],
        "init_bal_b": instance["init_bal_b"],
        "init_total": int(instance["init_bal_a"]) + int(instance["init_bal_b"]),
        "reserve_floor": instance["reserve_floor"],
        "available_slack": instance["available_slack"],
    }
    if seat in ("p1", "acct_a"):
        values["req_a"] = instance["req_a"]
    if seat in ("p1", "acct_b"):
        values["req_b"] = instance["req_b"]
    rendered = _fill(raw, values) + "\n"
    assert "tests/" not in rendered and "solution/" not in rendered
    assert all(cond not in rendered for cond in reference.CONDITIONS)
    assert str(instance["initial_rate"]) not in rendered
    assert str(instance["post_transfer_rate"]) not in rendered
    assert str(instance["rumor_rate"]) not in rendered
    return rendered


def _patch_task_toml(text: str) -> str:
    assert text.count(ENVIRONMENT) == 1 and "[environment.env]" not in text
    return text.replace(ENVIRONMENT, ENVIRONMENT + NO_BAKE)


def _description(name: str, condition: str, instance: dict[str, Any]) -> str:
    seats_label = ", ".join(str(s) for s in instance["seats"])
    return (
        f"{name}, condition {condition}: seats [{seats_label}] settle dual-account ledger "
        f"{instance['task_id']} ({instance['domain']}) under coupled reserve floor {instance['reserve_floor']}."
    )


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    pol_scripts = {
        name: (TEMPLATE / "policies" / f"{name}.sh").read_text(encoding="utf-8")
        for name in reference.POLICIES
    }
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for cond in reference.CONDITIONS:
        for inst in reference.instances(config, cond):
            seats = [str(s) for s in inst["seats"]]
            seat_tokens = reference.tokens(config, inst)
            name = f"{config['name']}-i{int(inst['instance']):02d}"
            oracle_rew = round(reference.simulate(inst, seat_tokens, "oracle")["reward"], 6)
            expected_rew = 0.0 if cond == "indep-3" else 1.0
            assert oracle_rew == expected_rew
            files = build.task_files(
                name=f"muster/{name}",
                description=_description(name, cond, inst),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={s: instruction(inst, s) for s in seats},
                world={
                    "world.py": world_src,
                    "instance.json": json.dumps(inst, indent=2) + "\n",
                },
                verify=verify_src,
                policies=pol_scripts,
                metadata={
                    "family": str(config["name"]),
                    "condition": cond,
                    "instance": int(inst["instance"]),
                    "oracle_reward": oracle_rew,
                    "exploits": [str(x) for x in config["exploits"]],
                },
                internet=False,
            )
            files["task.toml"] = _patch_task_toml(files["task.toml"])
            files["environment/Dockerfile"] = MAIN_DOCKERFILE
            files["tests/test.sh"] = TEST_SH
            out[f"{cond}/{name}"] = files
    assert len(out) == 40
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Generate skewledger_v1 Harbor tasks.")
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
