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
PLACEHOLDER_RE = re.compile(r"\{[a-z0-9_]+\}")

_ref_spec = importlib.util.spec_from_file_location("replan_v1_reference", FAMILY / "reference.py")
assert _ref_spec is not None and _ref_spec.loader is not None
reference = importlib.util.module_from_spec(_ref_spec)
_ref_spec.loader.exec_module(reference)
build = reference.build


def _part(name: str) -> str:
    return (INSTRUCTIONS / f"{name}.md").read_text(encoding="utf-8").strip()


def render_instruction(instance: dict[str, Any], seat: str) -> str:
    seats = [str(s) for s in instance["seats"]]
    commit_part = "commit_fenced" if instance["epoch_fence"] else "commit_unfenced"
    if len(seats) == 1:
        text = _part("solo_frame")
        text = text.replace("{commit_doc}", _part(commit_part))
    else:
        role_part = "alert_role" if seat == instance["alert_seat"] else "worker_role"
        team_parts = [_part("chat")] if instance["channel"] else [_part("silent")]
        if instance["notices"]:
            team_parts.append(_part("named"))
        text = _part("frame")
        text = text.replace("{commit_doc}", _part(commit_part))
        text = text.replace("{role}", _part(role_part))
        text = text.replace("{team}", "\n\n".join(team_parts))
    stage1_commits = len(instance["epoch1_targets"][seat]) - len(instance["stale_targets"][seat])
    replacements = {
        "seat": seat,
        "your_records": json.dumps(instance["partitions"][seat]),
        "budget": instance["budget"],
        "trigger": instance["trigger_threshold"],
        "stage1_commits": stage1_commits,
    }
    for key, val in replacements.items():
        text = text.replace(f"{{{key}}}", str(val))
    assert not PLACEHOLDER_RE.search(text), PLACEHOLDER_RE.findall(text)
    assert "tests/" not in text
    if not instance["channel"]:
        assert "/msg" not in text and "/notice" not in text
    return text if text.endswith("\n") else text + "\n"


def _description(name: str, condition: str, instance: dict[str, Any]) -> str:
    seats = [str(s) for s in instance["seats"]]
    if len(seats) == 1:
        mode = "1 solo seat across all 72 records"
    elif not instance["channel"]:
        mode = "3 silent seats without a channel"
    elif instance["epoch_fence"]:
        mode = "3 seats with named /notices and epoch-fenced POST /commit"
    elif instance["notices"]:
        mode = "3 seats with named /notices and /msg"
    else:
        mode = "3 seats with free-text /msg"
    return (
        f"{name}, condition {condition}: {mode}, {instance['budget']} commits per seat, "
        "adapt to mid-flight Epoch 2 replan without committing stale Epoch 1 records."
    )


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
                description=_description(name, condition, instance),
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
                    "notices": bool(instance["notices"]),
                    "epoch_fence": bool(instance["epoch_fence"]),
                    "oracle_reward": oracle_reward,
                    "exploits": list(config["exploits"]),
                },
                internet=False,
            )
            assert files["task.toml"].count("[environment.env]") == 1
            assert 'COMPOSE_BAKE = "false"' in files["task.toml"]
            out[f"{condition}/{name}"] = files
    assert len(out) == 50
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
