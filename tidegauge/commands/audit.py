from __future__ import annotations

import argparse

from tidegauge import render
from tidegauge.audit import SEVERITIES, audit, worst
from tidegauge.commands._common import NAME_WIDTH, add_arms, add_common, findings_text, load
from tidegauge.model import Arm

DESCRIPTION = """``tidegauge audit``: can these numbers be trusted and compared?

Exit status: 0 = no error, 1 = at least 1 error (with --strict: or 1 warning).
"""
HELP = "check that the trials are complete, valid and comparable"


def configure(parser: argparse.ArgumentParser) -> None:
    add_arms(parser)
    add_common(parser, metric=False)
    parser.add_argument("--strict", action="store_true", help="exit with status 1 on warnings too")
    parser.add_argument(
        "--notes", action="store_true", help="list the notes too, not only errors and warnings"
    )


def build(arms: list[Arm]) -> dict:
    findings = audit(arms)
    return {
        "command": "audit",
        "arms": [
            {
                "name": arm.name,
                "tasks": len(arm.by_task()),
                "trials": len(arm.trials),
                "valid": sum(1 for t in arm.trials if t.valid),
            }
            for arm in arms
        ],
        "counts": {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES},
        "worst": worst(findings),
        "findings": [f.as_dict() for f in findings],
    }


def text(report: dict, *, notes: bool = False) -> str:
    rows = [
        [render.shorten(a["name"], NAME_WIDTH), a["tasks"], a["trials"], a["valid"]] for a in report["arms"]
    ]
    out = [render.table(["arm", "tasks", "trials", "valid"], rows, right=(1, 2, 3)), ""]
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
    report = build(load(args))
    print(render.to_json(report) if args.json else text(report, notes=args.notes))
    if report["worst"] == "error" or (args.strict and report["worst"] == "warn"):
        return 1
    return 0
