from __future__ import annotations

import argparse

from tidegauge import render
from tidegauge.audit import SEVERITIES, audit, worst
from tidegauge.commands._common import (
    NAME_WIDTH,
    add_arms,
    add_common,
    add_stall,
    findings_text,
    load,
)
from tidegauge.health import STALL_SECONDS, unmeasured
from tidegauge.model import Arm

DESCRIPTION = """``tidegauge audit``: can these numbers be trusted and compared?

Seat trials also get health checks: stalled seats (AgentTimeoutError and no agent
step in the last --stall-seconds of the run), lost seats (a non-zero exit code that
result.json does not record), seats with 0 model calls, seats at their turn cap,
seats that stop early, and trials that never reached the agent phase (Docker Hub
pull limit, build failure). The unmeasured column counts trials without a valid
reward and trials that a health check marks unmeasured.

Exit status: 0 = no error, 1 = at least 1 error (with --strict: or 1 warning).
"""
HELP = "check that the trials are complete, valid, healthy and comparable"


def configure(parser: argparse.ArgumentParser) -> None:
    add_arms(parser)
    add_common(parser, metric=False)
    add_stall(parser)
    parser.add_argument("--strict", action="store_true", help="exit with status 1 on warnings too")
    parser.add_argument(
        "--notes", action="store_true", help="list the notes too, not only errors and warnings"
    )


def build(arms: list[Arm], stall_seconds: float = STALL_SECONDS) -> dict:
    findings = audit(arms, stall_seconds)
    return {
        "command": "audit",
        "stall_seconds": stall_seconds,
        "arms": [
            {
                "name": arm.name,
                "tasks": len(arm.by_task()),
                "trials": len(arm.trials),
                "valid": sum(1 for t in arm.trials if t.valid),
                "unmeasured": sum(1 for t in arm.trials if unmeasured(t, stall_seconds)),
            }
            for arm in arms
        ],
        "counts": {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES},
        "worst": worst(findings),
        "findings": [f.as_dict() for f in findings],
    }


def text(report: dict, *, notes: bool = False) -> str:
    rows = [
        [render.shorten(a["name"], NAME_WIDTH), a["tasks"], a["trials"], a["valid"], a["unmeasured"]]
        for a in report["arms"]
    ]
    headers = ["arm", "tasks", "trials", "valid", "unmeasured"]
    out = [render.table(headers, rows, right=(1, 2, 3, 4)), ""]
    arms_by_code: dict[tuple[str, str], set] = {}
    for finding in report["findings"]:
        arms_by_code.setdefault((finding["severity"], finding["code"]), set()).add(finding["arm"])
    order = sorted(arms_by_code, key=lambda key: (SEVERITIES.index(key[0]), key[1]))
    if len(report["arms"]) > 1 and order:
        summary = [[severity, code, len(arms_by_code[(severity, code)])] for severity, code in order]
        out += [render.table(["severity", "finding", "arms"], summary, right=(2,)), ""]
    counts = report["counts"]
    out.append(
        ", ".join(
            render.count(counts[s], word)
            for s, word in zip(SEVERITIES, ("error", "warning", "note"), strict=True)
        )
    )
    listing = findings_text(report["findings"], severities=SEVERITIES if notes else SEVERITIES[:2])
    if listing:
        out.append(listing)
    if counts["note"] and not notes:
        out.append("  (--notes lists the notes)")
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    report = build(load(args), args.stall_seconds)
    print(render.to_json(report) if args.json else text(report, notes=args.notes))
    if report["worst"] == "error" or (args.strict and report["worst"] == "warn"):
        return 1
    return 0
