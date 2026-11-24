from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from worldwatch import __version__
from worldwatch.baselines import run_certify_baselines
from worldwatch.compare import run_compare
from worldwatch.detectors import run_detectors
from worldwatch.model import (
    DEFAULT_MAX_REJECT_RATIO,
    DEFAULT_STALL_SECONDS,
    InputError,
    parse_iso_utc,
)
from worldwatch.preflight import run_preflight
from worldwatch.render import (
    render_certify_baselines,
    render_compare,
    render_detectors,
    render_preflight,
    render_step_join,
    render_storyboard,
    render_tail,
    render_validity,
    to_json,
)
from worldwatch.step_join import DEFAULT_STEP_MARGIN_S, run_step_join
from worldwatch.storyboard import DEFAULT_MAX_STORYBOARD_TRIALS, run_storyboard
from worldwatch.stream import TailEngine, run_tail_loop
from worldwatch.validity import run_validity


def _pos_float(text: str) -> float:
    try:
        val = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a float: {text!r}") from None
    if not math.isfinite(val) or val <= 0.0:
        raise argparse.ArgumentTypeError(f"must be a positive finite float: {text!r}")
    return val


def _open_unit_float(text: str) -> float:
    try:
        val = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a float: {text!r}") from None
    if not math.isfinite(val) or not (0.0 < val < 1.0):
        raise argparse.ArgumentTypeError(f"must be strictly between 0.0 and 1.0: {text!r}")
    return val


def _ratio_float(text: str) -> float:
    try:
        val = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a float: {text!r}") from None
    if not math.isfinite(val) or not 0.0 <= val <= 1.0:
        raise argparse.ArgumentTypeError(f"must be between 0.0 and 1.0: {text!r}")
    return val


def _nonneg_float(text: str) -> float:
    try:
        val = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a float: {text!r}") from None
    if not math.isfinite(val) or val < 0.0:
        raise argparse.ArgumentTypeError(f"must be a non-negative finite float: {text!r}")
    return val


def _pos_int(text: str) -> int:
    try:
        val = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if val < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1: {text!r}")
    return val


def _nonneg_int(text: str) -> int:
    try:
        val = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if val < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative: {text!r}")
    return val


def _iso_moment(text: str) -> datetime:
    parsed = parse_iso_utc(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a valid ISO-8601 timestamp: {text!r}")
    return parsed


def _configure_tail(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("target", type=Path, help="Harbor/Muster/Bespoke job or trial directory")
    sub.add_argument("--interval", type=_pos_float, default=2.0, help="poll interval seconds")
    sub.add_argument("--once", action="store_true", help="poll once deterministically and exit")
    sub.add_argument("--follow", action="store_true", help="keep polling across multi-arm gaps until critical/STOP")
    sub.add_argument("--stall-seconds", type=_pos_float, default=DEFAULT_STALL_SECONDS)
    sub.add_argument("--alerts", type=Path, default=None, help="append deduplicated NDJSON alerts")
    sub.add_argument("--html", type=Path, default=None, help="atomic dark-theme HTML dashboard path")
    sub.add_argument("--stop-file", type=Path, default=None, help="atomic JSON halt file on critical tripwires")
    sub.add_argument("--max-reject-ratio", type=_ratio_float, default=DEFAULT_MAX_REJECT_RATIO)
    sub.add_argument("--max-cost-usd", type=_pos_float, default=None)
    sub.add_argument("--now", type=_iso_moment, default=None, help="fixed UTC timestamp for deterministic snapshots")
    sub.add_argument("--fail-on-alert", action="store_true", help="exit 1 on any alert (default exits 1 on critical/stop)")
    sub.add_argument("--json", action="store_true", help="emit JSON report")


def _cmd_tail(args: argparse.Namespace) -> int:
    engine = TailEngine(
        args.target,
        stall_seconds=args.stall_seconds,
        max_reject_ratio=args.max_reject_ratio,
        max_cost_usd=args.max_cost_usd,
        alerts_path=args.alerts,
        html_path=args.html,
        stop_file=args.stop_file,
    )
    report, code = run_tail_loop(
        engine,
        once=args.once,
        interval=args.interval,
        follow=args.follow,
        now=args.now,
        fail_on_alert=args.fail_on_alert,
    )
    sink_errs = report.get("sink_errors") or []
    if sink_errs and not report.get("stopped") and report.get("critical_alerts", 0) == 0:
        raise InputError("; ".join(str(e) for e in sink_errs))
    if sink_errs:
        print(f"worldwatch: {'; '.join(str(e) for e in sink_errs)}", file=sys.stderr)
    print(to_json(report) if args.json else render_tail(report))
    return code


def _cmd_detectors(args: argparse.Namespace) -> int:
    report = run_detectors(args.path)
    print(to_json(report) if args.json else render_detectors(report))
    return 0


def _cmd_validity(args: argparse.Namespace) -> int:
    report, code = run_validity(
        list(args.arms),
        max_unmeasured_rate=args.max_unmeasured_rate,
        alpha=args.alpha,
        stall_seconds=args.stall_seconds,
    )
    print(to_json(report) if args.json else render_validity(report))
    return code


def _cmd_certify(args: argparse.Namespace) -> int:
    report, code = run_certify_baselines(args.family_dir)
    if code == 3 and report.get("error"):
        print(f"worldwatch: {report['error']}", file=sys.stderr)
    print(to_json(report) if args.json else render_certify_baselines(report))
    return code


def _cmd_compare(args: argparse.Namespace) -> int:
    report, code = run_compare(
        list(args.arms),
        html_path=args.html,
        alpha=args.alpha,
        stall_seconds=args.stall_seconds,
        seed=args.seed,
    )
    print(to_json(report) if args.json else render_compare(report))
    return code


def _cmd_preflight(args: argparse.Namespace) -> int:
    report, code = run_preflight(
        [str(t) for t in args.targets],
        check_openrouter=args.check_openrouter,
        min_key_usd=args.min_key_usd,
        min_account_usd=args.min_account_usd,
    )
    print(to_json(report) if args.json else render_preflight(report))
    return code


def _configure_compare_and_preflight(subs: Any) -> None:
    p_cmp = subs.add_parser("compare", help="paired trial comparison (sign-flip, McNemar, attrition, HTML)")
    p_cmp.add_argument("arms", nargs="+", metavar="ARM", help="arm directory or LABEL=DIR")
    p_cmp.add_argument("--html", type=Path, default=None, help="atomic dark-theme comparison HTML report path")
    p_cmp.add_argument("--alpha", type=_open_unit_float, default=0.05)
    p_cmp.add_argument("--stall-seconds", type=_pos_float, default=DEFAULT_STALL_SECONDS)
    p_cmp.add_argument("--seed", type=_nonneg_int, default=0)
    p_cmp.add_argument("--json", action="store_true", help="emit JSON report")
    p_cmp.set_defaults(handler=_cmd_compare)

    p_pre = subs.add_parser("preflight", help="static task hygiene and dual OpenRouter budget gate")
    p_pre.add_argument("targets", nargs="*", type=Path, help="task directory, family build root, or task.toml")
    p_pre.add_argument("--check-openrouter", action="store_true", help="query OpenRouter /key and /credits")
    p_pre.add_argument("--min-key-usd", type=_pos_float, default=1.0)
    p_pre.add_argument("--min-account-usd", type=_pos_float, default=4.50)
    p_pre.add_argument("--json", action="store_true", help="emit JSON report")
    p_pre.set_defaults(handler=_cmd_preflight)


def _cmd_step_join(args: argparse.Namespace) -> int:
    report = run_step_join(args.path, margin_s=args.margin_s)
    print(to_json(report) if args.json else render_step_join(report))
    return 0


def _cmd_storyboard(args: argparse.Namespace) -> int:
    report = run_storyboard(
        args.path,
        html_path=args.html,
        margin_s=args.margin_s,
        max_trials=args.max_trials,
    )
    print(to_json(report) if args.json else render_storyboard(report))
    return 0


def _configure_step_join_and_storyboard(subs: Any) -> None:
    p_sj = subs.add_parser("step-join", help="join world/board events to ATIF agent steps")
    p_sj.add_argument("path", type=Path, help="trial or job directory")
    p_sj.add_argument("--margin-s", type=_nonneg_float, default=DEFAULT_STEP_MARGIN_S)
    p_sj.add_argument("--json", action="store_true", help="emit JSON report")
    p_sj.set_defaults(handler=_cmd_step_join)

    p_sb = subs.add_parser("storyboard", help="render CSP-strict interactive trial step storyboard")
    p_sb.add_argument("path", type=Path, help="trial or job directory")
    p_sb.add_argument("--html", type=Path, default=None, help="atomic dark-theme HTML storyboard path")
    p_sb.add_argument("--margin-s", type=_nonneg_float, default=DEFAULT_STEP_MARGIN_S)
    p_sb.add_argument("--max-trials", type=_pos_int, default=DEFAULT_MAX_STORYBOARD_TRIALS)
    p_sb.add_argument("--json", action="store_true", help="emit JSON report")
    p_sb.set_defaults(handler=_cmd_storyboard)


def _cmd_sim(args: argparse.Namespace) -> int:
    from worldwatch import sim

    return sim.dispatch_parsed(args)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="worldwatch",
        description="Live monitoring, behavioral detectors, validity gates, and baseline certification.",
    )
    root.add_argument("--version", action="version", version=f"worldwatch {__version__}")
    subs = root.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_tail = subs.add_parser(
        "tail",
        help="tail live or finished Olam/Muster and Bespoke runs; exit 0 healthy, 1 critical/stopped",
    )
    _configure_tail(p_tail)
    p_tail.set_defaults(handler=_cmd_tail)

    p_det = subs.add_parser("detectors", help="run deterministic behavioral event-log detectors")
    p_det.add_argument("path", type=Path, help="trial or job directory")
    p_det.add_argument("--json", action="store_true", help="emit JSON report")
    p_det.set_defaults(handler=_cmd_detectors)

    p_val = subs.add_parser("validity", help="dual-source liveness and exact 2x2 Fisher attrition gate")
    p_val.add_argument("arms", nargs="+", metavar="ARM", help="arm directory or LABEL=DIR")
    p_val.add_argument("--max-unmeasured-rate", type=_ratio_float, default=0.10)
    p_val.add_argument("--alpha", type=_open_unit_float, default=0.05)
    p_val.add_argument("--stall-seconds", type=_pos_float, default=DEFAULT_STALL_SECONDS)
    p_val.add_argument("--json", action="store_true", help="emit JSON report")
    p_val.set_defaults(handler=_cmd_validity)

    p_cert = subs.add_parser("certify-baselines", help="certify task family oracle, degenerates, exploits, and controls")
    p_cert.add_argument("family_dir", type=Path, help="task family directory with family.toml and reference.py")
    p_cert.add_argument("--json", action="store_true", help="emit JSON report")
    p_cert.set_defaults(handler=_cmd_certify)

    _configure_compare_and_preflight(subs)
    _configure_step_join_and_storyboard(subs)

    p_sim = subs.add_parser("sim", help="deterministic simulation testing (check, hunt, replay)")
    sim_subs = p_sim.add_subparsers(dest="sim_command", required=True)
    sim_subs.add_parser("check", help="replay fixed and regression seeds")
    p_hunt = sim_subs.add_parser("hunt", help="hunt for new failing seeds")
    p_hunt.add_argument("--seconds", type=int, required=True)
    p_hunt.add_argument("--start-seed", type=int, default=20261124)
    p_rep = sim_subs.add_parser("replay", help="replay one seed")
    p_rep.add_argument("seed", type=int)
    p_sim.set_defaults(handler=_cmd_sim)

    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (InputError, FileNotFoundError, OSError) as exc:
        print(f"worldwatch: {' '.join(str(exc).split())}", file=sys.stderr)
        return 3
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("worldwatch: interrupted", file=sys.stderr)
        return 130
