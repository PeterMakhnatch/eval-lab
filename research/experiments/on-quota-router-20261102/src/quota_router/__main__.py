"""Command line interface for quota router."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .activity import route_quota_ok
from .parser import fetch_live_usage, parse_usage, scrub_usage_text
from .router import DEFAULT_ROUTES_PATH, pick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quota_router",
        description="Quota-aware route selector with measured burn and hysteresis.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: pick
    pick_p = subparsers.add_parser("pick", help="Pick best route from candidates based on quota and burn.")
    pick_p.add_argument("candidates", nargs="+", help="Candidate routes in preference order.")
    pick_p.add_argument("--hours", "-H", type=float, default=1.0, help="Planned worker-hours (default: 1.0).")
    pick_p.add_argument("--routes", type=Path, default=DEFAULT_ROUTES_PATH, help="Path to routes.json.")
    pick_p.add_argument("--usage-file", type=Path, help="Path to cached/simulated omp usage text.")
    pick_p.add_argument("--last-route", type=str, help="Previously active route for hysteresis stickiness.")
    pick_p.add_argument("--state-file", type=Path, help="Path to state file for persistent hysteresis.")
    pick_p.add_argument("--json", action="store_true", help="Output JSON result.")

    # Subcommand: check
    check_p = subparsers.add_parser("check", help="Check whether a specific route has sufficient quota.")
    check_p.add_argument("route", help="Target route to check.")
    check_p.add_argument("--hours", "-H", type=float, default=1.0, help="Planned worker-hours (default: 1.0).")
    check_p.add_argument("--routes", type=Path, default=DEFAULT_ROUTES_PATH, help="Path to routes.json.")
    check_p.add_argument("--usage-file", type=Path, help="Path to cached/simulated omp usage text.")
    check_p.add_argument("--json", action="store_true", help="Output JSON result.")

    # Subcommand: parse
    parse_p = subparsers.add_parser("parse", help="Parse omp usage output and print extracted metrics.")
    parse_p.add_argument("--usage-file", type=Path, help="Path to usage text file (default: live omp usage).")
    parse_p.add_argument("--json", action="store_true", help="Output JSON snapshot.")

    # Subcommand: scrub
    scrub_p = subparsers.add_parser("scrub", help="Scrub sensitive info from usage text for fixtures.")
    scrub_p.add_argument("--usage-file", type=Path, help="Input usage text file (default: live omp usage).")
    scrub_p.add_argument("--output", "-o", type=Path, help="Output file (default: stdout).")

    args = parser.parse_args(argv)

    if args.command == "pick":
        usage_text = args.usage_file.read_text(encoding="utf-8") if args.usage_file else None
        res = pick(
            candidates=args.candidates,
            need_worker_hours=args.hours,
            usage_text=usage_text,
            routes_config=args.routes,
            last_route=args.last_route,
            state_file=args.state_file,
        )
        if args.json:
            out = {
                "route": res.route,
                "refused": res.refused,
                "reason": res.reason,
                "evaluations": {
                    k: {
                        "eligible": v.eligible,
                        "current_max_pct": v.current_max_pct,
                        "projected_max_pct": v.projected_max_pct,
                        "limiting_metric": v.limiting_metric,
                        "headroom_pct": v.headroom_pct,
                        "refusal_reason": v.refusal_reason,
                    }
                    for k, v in res.evaluations.items()
                },
            }
            print(json.dumps(out, indent=2))
        else:
            if res.refused:
                print(f"REFUSE: {res.reason}")
            else:
                print(res.route)
                print(f"  Reason: {res.reason}")
        return 1 if res.refused else 0

    if args.command == "check":
        usage_text = args.usage_file.read_text(encoding="utf-8") if args.usage_file else None
        ok, reason = route_quota_ok(
            route=args.route,
            worker_hours=args.hours,
            usage_text=usage_text,
            routes_config=args.routes,
        )
        if args.json:
            print(json.dumps({"ok": ok, "route": args.route, "hours": args.hours, "reason": reason}, indent=2))
        else:
            print(reason)
        return 0 if ok else 1

    if args.command == "parse":
        usage_text = args.usage_file.read_text(encoding="utf-8") if args.usage_file else fetch_live_usage()
        snap = parse_usage(usage_text)
        if args.json:
            out = {
                "metrics": {
                    k: {
                        "name": v.name,
                        "percent_used": v.percent_used,
                        "resets_in_str": v.resets_in_str,
                        "resets_in_seconds": v.resets_in_seconds,
                    }
                    for k, v in snap.metrics.items()
                },
                "balances_usd": snap.balances_usd,
            }
            print(json.dumps(out, indent=2))
        else:
            print("Extracted Metrics:")
            for k, v in snap.metrics.items():
                res_str = f" (resets in {v.resets_in_str})" if v.resets_in_str else ""
                print(f"  {k:15s} [{v.name:32s}]: {v.percent_used:5.1f}% used{res_str}")
            if snap.balances_usd:
                print("Balances:")
                for bk, bv in snap.balances_usd.items():
                    print(f"  {bk:20s}: ${bv:.2f}")
        return 0

    if args.command == "scrub":
        raw = args.usage_file.read_text(encoding="utf-8") if args.usage_file else fetch_live_usage()
        scrubbed = scrub_usage_text(raw)
        if args.output:
            args.output.write_text(scrubbed, encoding="utf-8")
        else:
            print(scrubbed)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
