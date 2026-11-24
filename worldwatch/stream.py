from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worldwatch._chain import verify_event_row_chain
from worldwatch._io import (
    append_ndjson_rows,
    atomic_write_json,
    discover_trials,
    read_regular_bytes,
    read_regular_text,
    read_tolerant_json_object,
    split_complete_jsonl_bytes,
    split_text_lines_strict,
    stat_regular_file,
)
from worldwatch._trial import load_trial_bundle
from worldwatch._tripwires import (
    build_seat_liveness,
    evaluate_cost_tripwire,
    evaluate_trial_tripwires,
    extract_seat_event_stats,
)
from worldwatch.dashboard import write_dashboard_html
from worldwatch.model import (
    CRITICAL_TRIPWIRES,
    DEFAULT_MAX_REJECT_RATIO,
    DEFAULT_STALL_SECONDS,
    GENESIS_HASH,
    MAX_ALERTS,
    MAX_EVENTS_PER_FILE,
    MAX_FILE_BYTES,
    Alert,
    InputError,
    TrialLive,
    parse_strict_json,
    stamp_utc,
)

MAX_TAIL_TICKS = 1_000_000


@dataclass
class JsonlCursor:
    path: Path
    require_seq: bool = False
    dev: int | None = None
    ino: int | None = None
    offset: int = 0
    partial_bytes: int = 0
    resets: int = 0
    line_count: int = 0
    chain_mode: bool | None = None
    prev_hash: str = GENESIS_HASH
    has_hmac: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def _reset_stream(self) -> None:
        self.offset = 0
        self.partial_bytes = 0
        self.line_count = 0
        self.chain_mode = None
        self.prev_hash = GENESIS_HASH
        self.has_hmac = False
        self.rows.clear()
        self.errors.clear()
        self.resets += 1

    def poll(self, hmac_key: str | None = None) -> int:
        info, serr = stat_regular_file(self.path)
        if serr is not None:
            if serr not in self.errors:
                self.errors.append(serr)
            return 0
        if info is None:
            if self.offset > 0 or self.rows:
                self._reset_stream()
                self.dev = None
                self.ino = None
            return 0
        if self.dev is not None and (
            info.st_dev != self.dev or info.st_ino != self.ino or info.st_size < self.offset
        ):
            self._reset_stream()
        snap, rerr = read_regular_bytes(self.path, offset=self.offset, max_bytes=MAX_FILE_BYTES)
        if rerr is not None or snap is None:
            if rerr and rerr not in self.errors:
                self.errors.append(rerr)
            return 0
        if self.dev is not None and (
            snap.dev != self.dev or snap.ino != self.ino or snap.size < self.offset
        ):
            self._reset_stream()
            snap, rerr = read_regular_bytes(self.path, offset=0, max_bytes=MAX_FILE_BYTES)
            if rerr is not None or snap is None:
                if rerr and rerr not in self.errors:
                    self.errors.append(rerr)
                return 0
        self.dev = snap.dev
        self.ino = snap.ino
        return self._consume_bytes(snap.data, hmac_key)

    def _consume_bytes(self, chunk: bytes, hmac_key: str | None) -> int:
        try:
            complete, trailing = split_complete_jsonl_bytes(chunk)
        except ValueError as exc:
            self.errors.append(str(exc))
            return 0
        consumed = len(chunk) - len(trailing)
        assert consumed >= 0
        self.offset += consumed
        self.partial_bytes = len(trailing)
        added = 0
        for raw_line in complete:
            if not raw_line.strip():
                continue
            self.line_count += 1
            try:
                line_txt = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                self.errors.append(f"{self.path.name} line {self.line_count}: invalid UTF-8")
                continue
            try:
                parsed = parse_strict_json(line_txt)
            except ValueError as exc:
                self.errors.append(f"{self.path.name} line {self.line_count}: {exc}")
                continue
            if not isinstance(parsed, dict):
                self.errors.append(f"{self.path.name} line {self.line_count}: row is not a JSON object")
                continue
            self.chain_mode, self.prev_hash, self.has_hmac, cerr = verify_event_row_chain(
                parsed,
                len(self.rows) + 1,
                self.prev_hash,
                self.chain_mode,
                self.has_hmac,
                self.require_seq,
                hmac_key,
            )
            if cerr is not None:
                self.errors.append(f"{self.path.name} {cerr}")
            if len(self.rows) < MAX_EVENTS_PER_FILE:
                self.rows.append(parsed)
                added += 1
        return added


def _config_mtime_utc(trial_dir: Path) -> datetime | None:
    for name in ("config.json", "lock.json"):
        info, err = stat_regular_file(trial_dir / name)
        if err is None and info is not None:
            return datetime.fromtimestamp(info.st_mtime, UTC)
    try:
        return datetime.fromtimestamp(os.lstat(trial_dir).st_mtime, UTC)
    except OSError:
        return None


def _load_existing_alert_ids(alerts_path: Path | None) -> set[str]:
    if alerts_path is None or not os.path.lexists(alerts_path):
        return set()
    text, err = read_regular_text(alerts_path, max_bytes=MAX_FILE_BYTES)
    if err is not None or text is None:
        return set()
    seen: set[str] = set()
    for line in split_text_lines_strict(text)[:MAX_ALERTS]:
        if not line.strip():
            continue
        try:
            row = parse_strict_json(line)
        except ValueError:
            continue
        if isinstance(row, Mapping) and isinstance(row.get("alert_id"), str):
            seen.add(row["alert_id"])
    return seen


def _write_or_keep_stop_file(
    stop_path: Path,
    target: Path,
    now_iso: str,
    critical: list[Alert],
) -> dict[str, Any]:
    assert critical
    if os.path.lexists(stop_path):
        existing, err = read_tolerant_json_object(stop_path)
        if err is None and existing is not None and existing.get("halted") is True:
            return existing
    record: dict[str, Any] = {
        "halted": True,
        "target": str(target),
        "triggered_at": now_iso,
        "codes": sorted({a.code for a in critical}),
        "alerts": [a.as_dict() for a in critical[:64]],
    }
    atomic_write_json(stop_path, record)
    return record


class TailEngine:
    def __init__(
        self,
        target: Path,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        max_reject_ratio: float = DEFAULT_MAX_REJECT_RATIO,
        max_cost_usd: float | None = None,
        alerts_path: Path | None = None,
        html_path: Path | None = None,
        stop_file: Path | None = None,
        hmac_key: str | None = None,
    ) -> None:
        if not os.path.exists(target):
            raise InputError(f"path does not exist: {target}")
        if not os.path.isdir(target):
            raise InputError(f"not a directory: {target}")
        if stall_seconds <= 0:
            raise InputError("--stall-seconds must be positive")
        if not 0.0 <= max_reject_ratio <= 1.0:
            raise InputError("--max-reject-ratio must be in [0.0, 1.0]")
        if max_cost_usd is not None and max_cost_usd <= 0:
            raise InputError("--max-cost-usd must be positive")
        self.target = Path(target)
        self.stall_seconds = stall_seconds
        self.max_reject_ratio = max_reject_ratio
        self.max_cost_usd = max_cost_usd
        self.alerts_path = alerts_path
        self.html_path = html_path
        self.stop_file = stop_file
        self.hmac_key = hmac_key
        self.cursors: dict[Path, JsonlCursor] = {}
        self.emitted_alert_ids: set[str] = _load_existing_alert_ids(alerts_path)

    def _cursor(self, path: Path, *, require_seq: bool) -> JsonlCursor:
        cur = self.cursors.get(path)
        if cur is None:
            cur = JsonlCursor(path=path, require_seq=require_seq)
            self.cursors[path] = cur
        return cur

    def _poll_trial_streams(
        self,
        trial_dir: Path,
        ecosystem: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool | None, str | None, int, int]:
        if ecosystem == "bespoke":
            b_cur = self._cursor(trial_dir / "agent" / "board-events.jsonl", require_seq=False)
            p_cur = self._cursor(trial_dir / "agent" / "proxy-usage.jsonl", require_seq=False)
            b_cur.poll(self.hmac_key)
            p_cur.poll(self.hmac_key)
            errs = [*b_cur.errors, *p_cur.errors]
            chain_ok: bool | None = (
                False if errs else (True if (b_cur.chain_mode is True or p_cur.chain_mode is True) else None)
            )
            return (
                list(b_cur.rows),
                list(p_cur.rows),
                chain_ok,
                errs[0] if errs else None,
                b_cur.partial_bytes + p_cur.partial_bytes,
                b_cur.resets + p_cur.resets,
            )
        ev_rels = ("verifier/events.jsonl", "logs/world/events.jsonl", "artifacts/logs/world/events.jsonl")
        px_rels = ("proxy-usage.jsonl", "artifacts/proxy-usage.jsonl", "artifacts/logs/proxy/proxy-usage.jsonl")
        ev_cursors = [self._cursor(trial_dir / r, require_seq=True) for r in ev_rels]
        px_cursors = [self._cursor(trial_dir / r, require_seq=False) for r in px_rels]
        for cur in (*ev_cursors, *px_cursors):
            cur.poll(self.hmac_key)
        primary_ev = next((c for c in ev_cursors if c.rows or c.errors), ev_cursors[0])
        primary_px = next((c for c in px_cursors if c.rows or c.errors), px_cursors[0])
        errs = [e for c in (*ev_cursors, *px_cursors) for e in c.errors]
        any_hashed = any(c.chain_mode is True for c in (*ev_cursors, *px_cursors))
        chain_ok = False if errs else (True if any_hashed else None)
        partial = sum(c.partial_bytes for c in (*ev_cursors, *px_cursors))
        resets = sum(c.resets for c in (*ev_cursors, *px_cursors))
        return list(primary_ev.rows), list(primary_px.rows), chain_ok, (errs[0] if errs else None), partial, resets

    def _inspect_one_trial(
        self,
        trial_dir: Path,
        ecosystem: str,
        now: datetime,
        now_iso: str,
    ) -> tuple[TrialLive, list[Alert]]:
        events, proxy_rows, chain_ok, chain_err, partial, resets = self._poll_trial_streams(trial_dir, ecosystem)
        bundle = load_trial_bundle(trial_dir, ecosystem, proxy_rows=proxy_rows or None)
        seat_counts, seat_latest, total_req, rejected_req = extract_seat_event_stats(
            events, ecosystem, bundle.facing_to_seat
        )
        seats_live = build_seat_liveness(
            bundle,
            seat_counts,
            seat_latest,
            now,
            self.stall_seconds,
            _config_mtime_utc(trial_dir),
        )
        if bundle.result_state == "unreadable":
            t_state = "unreadable"
        elif bundle.result_state == "missing":
            t_state = "running"
        elif bundle.reward_valid and bundle.reward is not None:
            t_state = "finished"
        else:
            t_state = "failed"
        ratio = (rejected_req / total_req) if total_req > 0 else 0.0
        live = TrialLive(
            trial=bundle.trial_name,
            path=trial_dir,
            ecosystem=ecosystem,
            state=t_state,
            result_state=bundle.result_state,
            reward=bundle.reward,
            reward_valid=bundle.reward_valid,
            cost_usd=bundle.cost_usd,
            events_count=len(events),
            rejected_count=rejected_req,
            reject_ratio=ratio,
            partial_bytes=partial,
            resets=resets,
            chain_ok=chain_ok,
            chain_error=chain_err,
            seats=seats_live,
        )
        alerts = evaluate_trial_tripwires(
            bundle,
            seats_live,
            events,
            total_req,
            rejected_req,
            chain_err,
            now_iso,
            self.max_reject_ratio,
        )
        return live, alerts

    def poll(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(UTC)
        now_iso = stamp_utc(moment) or ""
        discovered = discover_trials(self.target)
        trials_live: list[TrialLive] = []
        active_alerts: list[Alert] = []
        counts: Counter[str] = Counter()
        for trial_dir, ecosystem in discovered:
            live, alerts = self._inspect_one_trial(trial_dir, ecosystem, moment, now_iso)
            trials_live.append(live)
            counts[live.state] += 1
            active_alerts.extend(alerts)
        known_costs = [t.cost_usd for t in trials_live if t.cost_usd is not None]
        total_cost = round(sum(known_costs), 9) if known_costs else None
        cost_alert = evaluate_cost_tripwire(total_cost, self.max_cost_usd, now_iso)
        if cost_alert is not None:
            active_alerts.append(cost_alert)
        new_rows: list[Mapping[str, Any]] = []
        for alert in active_alerts:
            if alert.alert_id not in self.emitted_alert_ids and len(self.emitted_alert_ids) < MAX_ALERTS:
                self.emitted_alert_ids.add(alert.alert_id)
                new_rows.append(alert.as_dict())
        if self.alerts_path is not None and new_rows:
            append_ndjson_rows(self.alerts_path, new_rows)
        critical = [a for a in active_alerts if a.code in CRITICAL_TRIPWIRES]
        stopped = bool(self.stop_file and os.path.lexists(self.stop_file))
        stop_record: dict[str, Any] | None = None
        if critical and self.stop_file is not None:
            stop_record = _write_or_keep_stop_file(self.stop_file, self.target, now_iso, critical)
            stopped = True
        report: dict[str, Any] = {
            "command": "tail",
            "target": str(self.target),
            "now": now_iso,
            "stall_seconds": self.stall_seconds,
            "max_reject_ratio": self.max_reject_ratio,
            "max_cost_usd": self.max_cost_usd,
            "counts": {
                "running": counts["running"],
                "finished": counts["finished"],
                "failed": counts["failed"],
                "unreadable": counts["unreadable"],
                "total": len(trials_live),
            },
            "total_events": sum(t.events_count for t in trials_live),
            "total_partial_bytes": sum(t.partial_bytes for t in trials_live),
            "total_resets": sum(t.resets for t in trials_live),
            "total_cost_usd": total_cost,
            "critical_alerts": len(critical),
            "warn_alerts": sum(1 for a in active_alerts if a.severity == "warn"),
            "new_alerts_emitted": len(new_rows),
            "stopped": stopped,
            "stop_record": stop_record,
            "alerts": [a.as_dict() for a in active_alerts],
            "trials": [t.as_dict() for t in trials_live],
        }
        if self.html_path is not None:
            write_dashboard_html(self.html_path, report)
        return report


def run_tail_loop(
    engine: TailEngine,
    *,
    once: bool,
    interval: float,
    now: datetime | None = None,
    fail_on_alert: bool = False,
    max_ticks: int = MAX_TAIL_TICKS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], int]:
    assert interval > 0 and 1 <= max_ticks <= MAX_TAIL_TICKS
    limit = 1 if once else max_ticks
    last_report: dict[str, Any] = {}
    for tick in range(limit):
        moment = now if now is not None else clock_fn()
        last_report = engine.poll(moment)
        if once:
            break
        if last_report["critical_alerts"] > 0 or last_report["stopped"]:
            break
        c = last_report["counts"]
        if c["total"] > 0 and c["running"] == 0 and c["unreadable"] == 0:
            break
        if tick + 1 < limit:
            sleep_fn(interval)
    bad = (
        last_report.get("critical_alerts", 0) > 0
        or last_report.get("stopped", False)
        or (fail_on_alert and len(last_report.get("alerts", ())) > 0)
    )
    return last_report, (1 if bad else 0)
