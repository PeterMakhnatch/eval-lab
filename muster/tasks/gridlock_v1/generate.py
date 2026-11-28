from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

FAMILY = Path(__file__).resolve().parent
TEMPLATE = FAMILY / "template"
INSTRUCTIONS = TEMPLATE / "instructions"
BUILD = FAMILY / "build"
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")

_ref_spec = importlib.util.spec_from_file_location("gridlock_v1_reference", FAMILY / "reference.py")
assert _ref_spec is not None and _ref_spec.loader is not None
reference = importlib.util.module_from_spec(_ref_spec)
_ref_spec.loader.exec_module(reference)
build = reference.build


def _part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def _station_line(instance: dict[str, Any], seat: str) -> str:
    zones = instance["stations"][seat]
    if len(zones) == len(reference.ZONES):
        return (
            "You start staged at `depot` and can reach all 3 zones (`A`, `B`, `C`) by calling "
            "`POST $WORLD/move_zone` (each zone transition uses 1 action)."
        )
    assert len(zones) == 1
    where = ", ".join(
        f"agent `{other}` in zone `{z[0]}`" for other, z in sorted(instance["stations"].items())
    )
    return (
        f"You are stationed in zone `{zones[0]}` (`current_zone = \"{zones[0]}\"`). "
        f"You can inspect (`GET $WORLD/zone`), dispatch crews, and energize buses only in zone `{zones[0]}`. "
        f"Station assignments: {where}."
    )


def render_instruction(instance: dict[str, Any], seat: str) -> str:
    agents = len(instance["seats"])
    paragraphs = [_part("alone") if agents == 1 else (_part("talk") if instance["channel"] else _part("silent"))]
    if instance["claim_locks"]:
        paragraphs.append(_part("claimlock"))
    rendered = _part("frame") + "\n"
    rendered = rendered.replace("{team}", "\n\n".join(paragraphs))
    rendered = rendered.replace("{station}", _station_line(instance, seat))
    values = {
        "seat": seat,
        "budget": str(instance["budget"]),
        "agents": str(agents),
        "thermal_cap": str(instance["thermal_cap"]),
        "ttl": str(reference.world.CLAIM_EXPIRY_TICKS),
    }
    for key, val in values.items():
        rendered = rendered.replace(f"{{{key}}}", val)
    assert not PLACEHOLDER_RE.search(rendered), PLACEHOLDER_RE.findall(rendered)
    for forbidden in ("tests/", *reference.CONDITIONS):
        assert forbidden not in rendered, forbidden
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _condition_label(instance: dict[str, Any]) -> str:
    if len(instance["seats"]) == 1:
        return "1 mobile seat without a channel"
    if instance["claim_locks"]:
        return "3 seats with a channel and claim interlocks"
    if instance["channel"]:
        return "3 seats with a message channel"
    return "3 seats without a message channel"


def tasks() -> dict[str, dict[str, str]]:
    config = reference.family()
    policies = {
        pol: (TEMPLATE / "policies" / f"{pol}.sh").read_text(encoding="utf-8")
        for pol in reference.POLICIES
    }
    world_src = (TEMPLATE / "world.py").read_text(encoding="utf-8")
    verify_src = (TEMPLATE / "verify.py").read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for condition in reference.CONDITIONS:
        for instance in reference.instances(config, condition):
            seats = [str(s) for s in instance["seats"]]
            seat_tokens = reference.tokens(config, instance)
            name = f"{config['name']}-i{int(instance['instance']):02d}"
            oracle_reward = round(reference.simulate(instance, seat_tokens, "oracle")["reward"], 6)
            files = build.task_files(
                name=f"muster/{name}",
                description=(
                    f"{name}, condition {condition}: {_condition_label(instance)}, "
                    f"{instance['budget']} actions per seat, restore a 3-zone cascading grid (kit v2)."
                ),
                seats=dict.fromkeys(seats, True),
                tokens=seat_tokens,
                instructions={s: render_instruction(instance, s) for s in seats},
                world={
                    "world.py": world_src,
                    "instance.json": json.dumps(instance, indent=2, sort_keys=True) + "\n",
                },
                verify=verify_src,
                policies=policies,
                metadata={
                    "family": config["name"],
                    "condition": condition,
                    "instance": int(instance["instance"]),
                    "seats_count": len(seats),
                    "channel": bool(instance["channel"]),
                    "claim_locks": bool(instance["claim_locks"]),
                    "oracle_reward": oracle_reward,
                    "exploits": list(config["exploits"]),
                },
                internet=False,
            )
            assert files["task.toml"].count("[environment.env]") == 1
            assert 'COMPOSE_BAKE = "false"' in files["task.toml"]
            out[f"{condition}/{name}"] = files
    assert len(out) == 40
    return out


def main(argv: list[str] | None = None, out: Path = BUILD) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        build.purge_bytecode(FAMILY)
        if args.clean and out.exists():
            for p in sorted(out.rglob("*"), reverse=True):
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
    status = build.emit(out, tasks(), args.check)
    if args.check:
        status = max(
            status,
            reference.check(reference.family()),
            build.check_no_bytecode(out),
        )
    else:
        build.purge_bytecode(FAMILY)
        status = max(status, build.check_no_bytecode(out), build.check_no_bytecode(FAMILY))
    return status


if __name__ == "__main__":
    sys.exit(main())
