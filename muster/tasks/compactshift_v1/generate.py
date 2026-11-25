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

_ref_spec = importlib.util.spec_from_file_location("compactshift_v1_reference", FAMILY / "reference.py")
assert _ref_spec is not None and _ref_spec.loader is not None
reference = importlib.util.module_from_spec(_ref_spec)
_ref_spec.loader.exec_module(reference)
build = reference.build

ROLE_SUMMARIES = {
    "p1": (
        "You operate as solo seat `p1` executing all three rollout stages sequentially: "
        "register the plan via `POST /plan`, refresh `GET /notices` after the mid-run epoch shift, "
        "submit the epoch-2 `POST /handoff`, run `POST /verify`, commit via `POST /commit`, and file `POST /report`."
    ),
    "planner": (
        "You operate as seat `planner` on a 3-seat rollout team (`planner`, `builder`, `verifier`). "
        "You own stage 1 (`POST /plan`) and, when `channel` is enabled, must publish `plan_seal` on `POST /msg` "
        "and keep up with teammate messages on `GET /msgs`."
    ),
    "builder": (
        "You operate as seat `builder` on a 3-seat rollout team (`planner`, `builder`, `verifier`). "
        "After `planner` triggers the epoch shift, you must read `GET /notices` (and `GET /msgs` when `channel` is enabled), "
        "preserve all base and epoch-2 shift constraints on `POST /handoff`, and share `handoff_id` and `handoff_digest`."
    ),
    "verifier": (
        "You operate as seat `verifier` on a 3-seat rollout team (`planner`, `builder`, `verifier`). "
        "Once `builder` completes `POST /handoff`, read `GET /msgs` and `GET /notices`, validate via `POST /verify`, "
        "finalize via `POST /commit`, and submit an accurate `POST /report`."
    ),
}


def render_instruction(instance: dict[str, Any], seat: str) -> str:
    frame = (INSTRUCTIONS / "frame.md").read_text(encoding="utf-8")
    values = {
        "seat": seat,
        "task_id": str(instance["task_id"]),
        "domain": str(instance["domain"]),
        "role_summary": ROLE_SUMMARIES[seat],
    }
    rendered = frame
    for key, val in values.items():
        rendered = rendered.replace(f"{{{key}}}", val)
    assert not PLACEHOLDER_RE.search(rendered), PLACEHOLDER_RE.findall(rendered)
    for forbidden in (
        "tests/",
        str(instance["plan_seal"]),
        str(instance["invariant_token"]),
        str(instance["handoff_digest"]),
        str(instance["verification_digest"]),
        *reference.CONDITIONS,
    ):
        assert forbidden not in rendered, forbidden
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _description(name: str, condition: str, instance: dict[str, Any]) -> str:
    seats_str = ", ".join(str(s) for s in instance["seats"])
    return (
        f"{name}, condition {condition}: seats [{seats_str}] coordinate mid-run epoch shift "
        f"and verified handoff commit for rollout {instance['task_id']} ({instance['domain']})."
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
                    "fenced": bool(instance["fenced"]),
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
