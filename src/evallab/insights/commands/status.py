"""Plain status of the engine: is it alive, what is each lens waiting for, what came out."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evallab.insights.commands._common import add_state_dir, emit, state_dir
from evallab.insights.config import EngineConfig, load_config
from evallab.insights.engine import rederive_sample
from evallab.insights.models import InsightStatus
from evallab.insights.store import DB_NAME, InsightStore

#: One line per lens saying what the role does (status output is for people).
LENS_ROLES: dict[str, str] = {
    "extractor": "extractor (reads reports into checked claims)",
    "skeptic": "skeptic (challenges every experiment's claims)",
    "connector": "connector (finds one mechanism behind several experiments)",
    "anomaly": "anomaly analyst (explains open statistical signals)",
    "theorist": "theorist (testable explanations)",
    "ideator": "ideator (next experiments)",
    "referee": "referee (a different model family grades each card)",
    "synthesizer": "synthesizer (ranks, merges, resolves predictions)",
    "rederiver": "rederiver (independent receipt check)",
    "twinmaker": "twinmaker (plants hidden flawed controls for referee calibration)",
}
#: Failure-ish event kinds worth showing an operator.
ERROR_EVENTS = {
    "tick_failed", "tick_step_failed", "pass_collect_failed", "pass_tampered",
    "mirror_refresh_failed", "board_failed", "route_served_other_model",
}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="Engine health, ledger counts and what each lens waits for")
    add_state_dir(parser)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=_cmd)


def _pid_alive(pid: object) -> bool | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _since(store: InsightStore, lens: str) -> datetime | None:
    done = store.list_passes(lens=lens, statuses=["succeeded"], limit=1)
    return done[0].created_at if done else None


def _skeptic_counts(store: InsightStore) -> tuple[int, int]:
    """(experiments with live claims, how many the skeptic reviewed at their current claims)."""
    candidates = store.experiment_review_candidates()
    reviewed = store.coverage_digests("skeptic")
    done = sum(1 for c in candidates if reviewed.get(str(c["exp_id"])) == c["claims_digest"])
    return len(candidates), done


def _coverage(store: InsightStore) -> dict[str, object]:
    claims = store.list_claims(include_failed=True)
    recipe_backed = [c for c in claims if c.claim.recipe is not None]
    statuses: dict[str, int] = {}
    for record in claims:
        key = record.recompute.status if record.recompute is not None else "no recipe"
        statuses[key] = statuses.get(key, 0) + 1
    rederivations = store.list_rederivations()
    compared = [r for r in rederivations if r.agrees is not None]
    total, reviewed_now = _skeptic_counts(store)
    return {
        "claims": len(claims),
        "recipe_backed": len(recipe_backed),
        "recompute": statuses,
        "rederivations": len(rederivations),
        "rederivations_comparable": len(compared),
        "rederivations_agree": sum(1 for r in compared if r.agrees),
        "experiments": total,
        "experiments_reviewed_by_skeptic": reviewed_now,
    }


def lens_progress(store: InsightStore, config: EngineConfig, lens: str) -> dict[str, object]:
    """What this lens needs before it runs, with the current and required counts."""
    schedule = config.lenses.get(lens)  # type: ignore[call-overload]
    role = LENS_ROLES.get(lens, lens)
    if schedule is None or not schedule.enabled:
        return {"role": role, "state": "disabled", "text": "disabled in config.yaml"}
    last = _since(store, lens)
    if lens == "extractor":
        counts = store.section_status_counts()
        pending = counts.get("pending", 0)
        text = (
            f"{pending} report sections waiting, {counts.get('in_flight', 0)} being read, "
            f"{counts.get('extracted', 0)} done, {counts.get('failed', 0)} gave up"
        )
        return {"role": role, "have": pending, "need": 1, "text": text}
    if lens == "skeptic":
        total, done = _skeptic_counts(store)
        text = f"{done} of {total} experiments reviewed at their current claims; {total - done} waiting"
        return {"role": role, "have": total - done, "need": 1, "text": text}
    if lens == "anomaly":
        have = len(store.list_signals(status="open", limit=100_000))
        need = max(1, schedule.min_open_signals)
        return {"role": role, "have": have, "need": need, "text": f"{have}/{need} open signals"}
    if lens == "referee":
        have = len(store.list_insights(statuses=["checked"])) + len(store.pending_canaries())
        need = max(1, schedule.min_pending)
        return {"role": role, "have": have, "need": need, "text": f"{have}/{need} cards waiting for grading"}
    if lens == "twinmaker":
        accepted = store.list_insights(statuses=["refereed"])
        used = store.canary_source_ids()
        fresh = [record for record in accepted if record.insight_id not in used]
        return {
            "role": role, "have": len(fresh), "need": max(4, schedule.min_new_insights),
            "text": f"{len(accepted)} accepted cards; {len(fresh)} still need a hidden twin",
        }
    if lens in {"theorist", "ideator", "synthesizer"}:
        statuses: list[InsightStatus] = (
            ["refereed"] if lens != "synthesizer" else ["refereed", "needs_revision", "rejected"]
        )
        have = len(store.list_insights(statuses=statuses, graded_since=last))
        need = max(1, schedule.min_new_insights)
        noun = "referee-approved cards" if lens != "synthesizer" else "newly graded cards"
        return {"role": role, "have": have, "need": need, "text": f"{have}/{need} {noun} since its last pass"}
    if lens == "connector":
        have = len(store.list_claims(since=last))
        need = max(1, schedule.min_new_claims)
        return {"role": role, "have": have, "need": need, "text": f"{have}/{need} new claims since its last pass"}
    if lens == "rederiver":
        backed = [
            c.claim_id for c in store.list_claims()
            if c.claim.recipe is not None and c.recompute is not None
        ]
        if not backed:
            return {
                "role": role, "have": 0, "need": max(1, schedule.min_new_claims),
                "text": "no receipt-backed claims yet (no claim carries a recipe), so no audit is possible",
            }
        done = store.rederived_claim_ids()
        sample = [cid for cid in rederive_sample(backed) if cid not in done]
        need = max(1, schedule.min_new_claims)
        return {
            "role": role, "have": len(sample), "need": need,
            "text": f"{len(sample)}/{need} sampled claims to re-derive ({len(backed)} receipt-backed claims)",
        }
    return {"role": role, "text": ""}


def collect_status(root: Path, *, now: datetime) -> dict[str, object]:
    heartbeat_path = root / "heartbeat.json"
    heartbeat = json.loads(heartbeat_path.read_text("utf-8")) if heartbeat_path.exists() else None
    age_minutes = None
    if heartbeat is not None and heartbeat.get("at"):
        at = datetime.fromisoformat(heartbeat["at"])
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        age_minutes = round((now - at).total_seconds() / 60, 1)
    tick_seconds = int(heartbeat.get("tick_seconds", 300)) if heartbeat else 300
    # A healthy engine beats at least once per tick; allow three missed ticks.
    stale_after = max(3 * tick_seconds / 60, 5.0)
    pid_alive = _pid_alive(heartbeat.get("pid")) if heartbeat else None
    with InsightStore.open_readonly(root / DB_NAME) as store:
        try:
            config = load_config(root)
        except Exception:  # noqa: BLE001 - a broken config must not hide ledger status
            config = None
        last_tick_raw = store.get_meta("last_tick")
        last_tick = json.loads(last_tick_raw) if last_tick_raw else {}
        day_ago = now - timedelta(hours=24)
        passes = store.list_passes(since=day_ago)
        by_lens: dict[str, dict[str, int]] = {}
        for record in passes:
            by_lens.setdefault(record.lens, {}).setdefault(record.status, 0)
            by_lens[record.lens][record.status] += 1
        failures = [
            {"pass_id": r.pass_id, "lens": r.lens, "status": r.status, "error": r.error}
            for r in passes
            if r.status in {"failed", "invalid_output", "timed_out", "parked_quota"}
        ][:10]
        recent_errors = [
            {"at": event["at"], "kind": event["kind"], "detail": event["detail"]}
            for event in store.events(limit=200)
            if event["kind"] in ERROR_EVENTS
        ][:10]
        lenses = (
            {lens: lens_progress(store, config, lens) for lens in LENS_ROLES}
            if config is not None else {}
        )
        waiting = last_tick.get("waiting", {})
        for lens, entry in lenses.items():
            entry["waiting"] = waiting.get(lens, "")
        parked_raw = store.get_meta("parked_routes")
        parked = {
            route: until for route, until in json.loads(parked_raw).items() if until > now.isoformat()
        } if parked_raw else {}
        alive = (
            age_minutes is not None and age_minutes < stale_after and pid_alive is not False
        )
        return {
            "state_dir": str(root),
            "engine_alive": alive,
            "heartbeat_age_minutes": age_minutes,
            "heartbeat_stale_after_minutes": stale_after,
            "engine_pid": heartbeat.get("pid") if heartbeat else None,
            "engine_pid_alive": pid_alive,
            "running": heartbeat.get("running", []) if heartbeat else [],
            "waiting": waiting,
            "lenses": lenses,
            "last_tick_errors": last_tick.get("errors", []),
            "recent_errors": recent_errors,
            "parked_routes": parked,
            "sources": len(store.list_sources()),
            "sections": store.section_status_counts(),
            "claims": store.claim_counts(),
            "coverage": _coverage(store),
            "open_signals": len(store.list_signals(status="open", limit=100_000)),
            "insights": store.insight_counts(),
            "insights_needing_recheck": store.insight_recheck_count(),
            "referee_calibration": store.calibration_rates(now=now),
            "predictions": {
                status: len(store.list_predictions(status=status))
                for status in ("open", "hit", "miss", "untestable")
            },
            "rederivations": {
                "total": len(store.list_rederivations()),
                "agree": sum(1 for r in store.list_rederivations() if r.agrees),
            },
            "passes_24h": by_lens,
            "recent_failures": failures,
            "board": str(root / "board" / "board.md"),
            "headline": store.get_meta("board_headline"),
        }


def _counts(values: object) -> str:
    if not isinstance(values, dict) or not values:
        return "none"
    return ", ".join(f"{count} {name}" for name, count in sorted(values.items()))


def _cmd(args: argparse.Namespace, root: Path, *, harbor: object | None = None) -> int:
    target = state_dir(args)
    if not (target / DB_NAME).exists():
        print(f"no insight ledger at {target}; run `evallab insights init` first")
        return 2
    status = collect_status(target, now=datetime.now(UTC))
    if args.json:
        emit(status)
        return 0
    age = status["heartbeat_age_minutes"]
    if status["engine_alive"]:
        alive = f"running (last heartbeat {age} min ago)"
    elif age is None:
        alive = "NOT running (no heartbeat file)"
    elif status["engine_pid_alive"] is False:
        alive = f"NOT running (process {status['engine_pid']} is gone; last heartbeat {age} min ago)"
    else:
        alive = (
            f"STALE (last heartbeat {age} min ago; expected within "
            f"{status['heartbeat_stale_after_minutes']:g} min)"
        )
    print(f"Insight engine: {alive}; state {status['state_dir']}")
    if status["headline"]:
        print(f"Board headline: {status['headline']}")
    print(
        f"Claims: {_counts(status['claims'])}. Cards: {_counts(status['insights'])}. "
        f"Open signals: {status['open_signals']}."
    )
    print(f"Report sections: {_counts(status['sections'])}. Predictions: {_counts(status['predictions'])}.")
    calibration = status["referee_calibration"]
    if isinstance(calibration, dict):
        twin, original = calibration["twin"], calibration["original"]
        if isinstance(twin, dict) and isinstance(original, dict):
            print(
                f"Referee calibration (30 days): caught {twin['k']} of {twin['n']} planted "
                f"flawed cards; accepted {original['k']} of {original['n']} original controls."
            )
    coverage = status["coverage"]
    if isinstance(coverage, dict):
        print(
            f"Receipt coverage: {coverage['recipe_backed']} of {coverage['claims']} claims carry a "
            f"recipe; recomputed: {_counts(coverage['recompute'])}. Independent re-derivations: "
            f"{coverage['rederivations']} ({coverage['rederivations_agree']} of "
            f"{coverage['rederivations_comparable']} comparable agree)."
        )
        print(
            f"Skeptic coverage: {coverage['experiments_reviewed_by_skeptic']} of "
            f"{coverage['experiments']} experiments reviewed at their current claims."
        )
    if status["insights_needing_recheck"]:
        print(f"{status['insights_needing_recheck']} cards cite claims that changed and await a re-check.")
    running = status["running"]
    if isinstance(running, list) and running:
        print("Running now: " + ", ".join(f"{r['lens']} ({r['route']})" for r in running))
    parked = status["parked_routes"]
    if isinstance(parked, dict) and parked:
        print("Parked routes: " + ", ".join(f"{route} until {until}" for route, until in parked.items()))
    lenses = status["lenses"]
    if isinstance(lenses, dict) and lenses:
        print("What each analyst is waiting for:")
        for entry in lenses.values():
            if not isinstance(entry, dict):
                continue
            blocked = f" [{entry['waiting']}]" if entry.get("waiting") else ""
            print(f"  {entry['role']}: {entry.get('text', '')}{blocked}")
    errors = status["last_tick_errors"]
    if isinstance(errors, list) and errors:
        print("Last tick errors:")
        for error in errors:
            print(f"  {error}")
    recent = status["recent_errors"]
    if isinstance(recent, list) and recent:
        print("Recent engine problems:")
        for item in recent[:5]:
            print(f"  {item['at']} {item['kind']}: {json.dumps(item['detail'])[:200]}")
    failures = status["recent_failures"]
    if isinstance(failures, list) and failures:
        print("Recent failed passes:")
        for failure in failures:
            print(f"  {failure['lens']}: {failure['status']} {failure['error'] or ''}")
    print(f"Board: {status['board']}")
    return 0
