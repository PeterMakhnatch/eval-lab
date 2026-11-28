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
MODE_LINE = "MODE=oracle\n"
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")

_spec = importlib.util.spec_from_file_location("audit_v1_reference", FAMILY / "reference.py")
assert _spec is not None and _spec.loader is not None
reference: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
build: Any = reference.build


def _part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def _policies() -> dict[str, str]:
    oracle_sh = (TEMPLATE / "policies" / "oracle.sh").read_text(encoding="utf-8")
    assert oracle_sh.count(MODE_LINE) == 1
    return {
        "oracle": oracle_sh,
        "nop": (TEMPLATE / "policies" / "nop.sh").read_text(encoding="utf-8"),
        "naive": oracle_sh.replace(MODE_LINE, "MODE=naive\n"),
        "spoof": (TEMPLATE / "policies" / "spoof.sh").read_text(encoding="utf-8"),
    }


def _workspace_note(instance: dict[str, Any], seat: str) -> str:
    assigned = instance["workspace_services"][seat]
    if len(assigned) == 3:
        return (
            "Your container workspace `/home/agent/workspace` contains all 3 microservice codebases: "
            "`/home/agent/workspace/service_a/auth_gateway.py`, "
            "`/home/agent/workspace/service_b/ledger_worker.py`, "
            "and `/home/agent/workspace/service_c/webhook_cache.py`."
        )
    assert len(assigned) == 1
    svc = assigned[0]
    peers = ", ".join(
        f"`{other}` holds `/home/agent/workspace/{svcs[0]}`"
        for other, svcs in sorted(instance["workspace_services"].items())
        if other != seat
    )
    return (
        f"Your container workspace `/home/agent/workspace` contains ONLY `/home/agent/workspace/{svc}` "
        f"(peer partitions: {peers}; neither peer files nor `/world` source are mounted in your container)."
    )


def _instruction(instance: dict[str, Any], seat: str) -> str:
    text = _part("frame") + "\n"
    text = text.replace("{condition_rules}", _part(str(instance["condition"])))
    text = text.replace("{workspace_note}", _workspace_note(instance, seat))
    for key, value in {
        "seat": seat,
        "submit_budget": instance["submit_budget"],
        "global_submit_budget": instance["global_submit_budget"],
    }.items():
        text = text.replace(f"{{{key}}}", str(value))
    assert not PLACEHOLDER_RE.search(text), PLACEHOLDER_RE.findall(text)
    assert "tests/" not in text
    svcs = instance["services"]
    all_tags = {
        "service_a": str(svcs["service_a"]["realm_prefix"]),
        "service_b": str(svcs["service_b"]["tenant_code"]),
        "service_c": str(svcs["service_c"]["channel_id"]),
    }
    for tag_val in all_tags.values():
        assert tag_val not in text
    return text


def _condition_note(instance: dict[str, Any]) -> str:
    if len(instance["seats"]) == 1:
        return "1 seat with all 3 services, 4-submission cap"
    if instance["claim_locks"]:
        return "3 partitioned seats with message channel and claim locks"
    if instance["channel"]:
        return "3 partitioned seats with message channel"
    return "3 partitioned seats without a message channel"


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    assert tuple(config["conditions"]) == reference.CONDITIONS
    scripts = _policies()
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in config["conditions"]:
        for instance in reference.instances(config, condition):
            seats = [str(s) for s in instance["seats"]]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{int(instance['instance']):02d}"
            oracle_reward = round(reference.simulate(instance, seat_tokens, "oracle")["reward"], 6)
            seat_files = {seat: reference.seat_workspace_files(instance, seat) for seat in seats}
            files = build.task_files(
                name=f"muster/{name}",
                description=(
                    f"{name}, condition {condition}: {_condition_note(instance)}, "
                    "audit 6 microservice discrepancies via witness verification (kit v2)."
                ),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={seat: _instruction(instance, seat) for seat in seats},
                world={
                    "world.py": world_src,
                    "instance.json": json.dumps(instance, indent=2, sort_keys=True) + "\n",
                },
                verify=verify_src,
                policies=scripts,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": int(instance["instance"]),
                    "kit": "v2",
                    "channel": bool(instance["channel"]),
                    "claim_locks": bool(instance["claim_locks"]),
                    "oracle_reward": oracle_reward,
                    "exploits": list(config["exploits"]),
                },
                seat_files=seat_files,
                internet=False,
            )
            assert files["task.toml"].count("[environment.env]") == 1
            assert 'COMPOSE_BAKE = "false"' in files["task.toml"]
            out[f"{condition}/{name}"] = files
    assert len(out) == 40
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser(description="Write the task directories of audit_v1.")
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
        status = max(status, build.check_no_bytecode(out), build.check_no_bytecode(FAMILY))
    return status


if __name__ == "__main__":
    sys.exit(main())
