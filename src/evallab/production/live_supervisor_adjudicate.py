"""Live Supervisor adjudication (LS-3): a blind strong reader re-decides sampled decisions.

Design: ``docs/live-supervisor.md`` (an action is enabled only after its adjudicated
precision is 0.9 or better). Input is decision rows, either replay rows from
``live_supervisor_replay`` (keyed by ``case_id``, carrying ``set`` and ``label_action``)
or live ``trace-findings/v1`` records from ``decisions-v1.jsonl`` (keyed by
``run_id@observed_at``, unlabelled), plus one observation *window* per row:

    {"key": <row key>, "subject": {"kind", "owner", "killable"},
     "observed_at", "metrics": {...}, "actor_inputs_available": bool,
     "events": [{"index", "at", "source", "text"}, ...], "triggers": [...]}

:func:`window_record` writes that shape from an :class:`~live_supervisor.Observation`;
``live_supervisor_windows`` persists one per live decision (``<root>/windows-v1.jsonl``).

:func:`sample` draws a stratified, capped, deterministic sample. :func:`adjudicate`
shows the adjudicator the subject (kind, owner, killable), the trigger hits, the metrics
and the event window. It never shows the label, the set or case name, the subject id,
the supervisor's action, proposal or reason, or a dataset or benchmark name (those are
redacted from all text). Action and trigger order is shuffled per row with the seed.
The reply is one strict JSON object. :func:`score` gives per-action precision and recall
of the supervisor against the adjudicator, adjudicator-vs-label agreement (Cohen's kappa)
where a label exists, and the list of false stops.

Calls go only through ``run_atlas.GatewayTransport`` (subscription routes); a reply
served by any other model than the requested one is rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.production import live_supervisor as ls
from evallab.production.run_atlas import (
    GatewayTransport,
    Transport,
    TransportError,
    bound,
    redact_sources,
    require_subscription_route,
)

RECORD_SCHEMA = "live-supervisor-adjudication/v1"
PROMPT_VERSION = "live-supervisor-adjudicate/1"
ADJUDICATOR_MODEL = "anthropic/claude-opus-5-5"
DEFAULT_TOTAL = 120
DEFAULT_PER_CELL = 12
#: The last events of the window shown to the adjudicator; each is bounded head-and-tail.
WINDOW_EVENTS = 40
EVENT_CHARS = 2_000
DETAIL_CHARS = 400
METRIC_CHARS = 300
REASON_WORDS = 40
MAX_OUTPUT_TOKENS = 4_000
REPLY_EXCERPT_CHARS = 500
UNLABELLED = "unlabelled"
REPLY_KEYS = frozenset({"action", "reason", "confidence"})
#: Row and metric keys that carry the label, the set or the supervisor's decision.
HIDDEN_KEYS = frozenset({
    "label", "label_action", "label_kind", "label_positive", "set", "case_id", "action", "proposal",
    "reason", "decision", "verdict", "action_match", "alarm_match", "expected", "expected_action",
})

MEANINGS = {
    "continue": "slow or odd, but progressing; do nothing",
    "nudge": "stuck but recoverable; send the owner a note to change course",
    "kill_and_retry": "agent-side stall, loop or runaway; stop it and requeue once",
    "kill_and_mark_infra": "the environment or backend is dead; stop it and record infra",
    "flag_cheating": "integrity problem (tampering with tests or grading, using answers); send an evidence card",
    "alert_research": "needs a human or another lane; write a report",
}

SYSTEM = (
    "You supervise automated work while it runs: agent trials, cloud jobs, services and "
    "coding-agent chat lanes. For one subject you see what deterministic monitors flagged "
    "and its most recent events. Decide what a careful operator should do now. Read the "
    "events themselves; a monitor firing is not proof of a problem."
)


# --------------------------------------------------------------------------- rows


def row_key(row: Mapping[str, Any]) -> str:
    """``case_id`` for replay rows; ``run_id@observed_at`` for live decision records."""
    if row.get("case_id"):
        return str(row["case_id"])
    if row.get("run_id"):
        return f"{row['run_id']}@{row['observed_at']}" if row.get("observed_at") else str(row["run_id"])
    raise ValueError("a decision row needs case_id or run_id")


def _subject_kind(row: Mapping[str, Any]) -> str:
    subject = row.get("subject")
    if isinstance(subject, Mapping) and subject.get("kind"):
        return str(subject["kind"])
    return str(row.get("subject_kind") or "unknown")


def label_action(row: Mapping[str, Any]) -> str | None:
    label = row.get("label_action")
    return label if label in ls.ACTIONS else None


def stratum(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """(set, or subject kind when there is no set; decided action; label agreement)."""
    label = label_action(row)
    agreement = UNLABELLED if label is None else ("agree" if label == row["action"] else "disagree")
    return (str(row.get("set") or _subject_kind(row)), str(row["action"]), agreement)


def _usable(row: Mapping[str, Any]) -> bool:
    return not row.get("error") and not row.get("skipped") and row.get("action") in ls.ACTIONS


def sample(rows: Iterable[Mapping[str, Any]], *, per_cell: int = DEFAULT_PER_CELL,
           total: int = DEFAULT_TOTAL, seed: int = 0) -> list[dict[str, Any]]:
    """At most ``per_cell`` rows per :func:`stratum` and ``total`` overall, deterministic.

    Cells are filled round-robin so every cell is represented before any cell gets a
    second row. Error, skipped and duplicate-key rows are dropped. The result is shuffled
    so the adjudicator does not read cells in blocks.
    """
    if per_cell < 1 or total < 1:
        raise ValueError("per_cell and total must be positive")
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for row in rows:
        if _usable(row) and row_key(row) not in seen:
            seen.add(row_key(row))
            cells[stratum(row)].append(dict(row))
    rng = random.Random(seed)
    order = sorted(cells)
    drawn: list[list[dict[str, Any]]] = []
    for cell in order:
        members = sorted(cells[cell], key=row_key)
        rng.shuffle(members)
        drawn.append(members[:per_cell])
    picked: list[dict[str, Any]] = []
    for depth in range(per_cell):
        for members in drawn:
            if depth < len(members) and len(picked) < total:
                picked.append(members[depth])
    rng.shuffle(picked)
    return picked


# --------------------------------------------------------------------------- windows


def window_record(key: str, observation: ls.Observation,
                  hits: Sequence[ls.TriggerHit] = ()) -> dict[str, Any]:
    """The ``--windows`` JSONL shape for one observation (what :func:`adjudicate` reads)."""
    subject = observation.subject
    return {
        "key": key,
        "subject": {"kind": subject.kind, "owner": subject.owner, "killable": subject.killable},
        "observed_at": observation.observed_at.isoformat(),
        "metrics": dict(observation.metrics),
        "actor_inputs_available": observation.actor_inputs_available,
        "events": [{"index": e.index, "at": None if e.at is None else e.at.isoformat(),
                    "source": e.source, "text": e.text} for e in observation.events],
        "triggers": [h.as_dict() for h in hits],
    }


def _blind(text: Any, limit: int) -> str:
    return redact_sources(bound(str(text), limit))


def _metrics_text(metrics: Mapping[str, Any]) -> list[str]:
    lines = []
    for name in sorted(metrics):
        if name.lower() in HIDDEN_KEYS:
            continue
        value = metrics[name]
        rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        lines.append(f"- {redact_sources(str(name))}: {_blind(rendered, METRIC_CHARS)}")
    return lines or ["- (none)"]


def build_messages(row: Mapping[str, Any], window: Mapping[str, Any], *, seed: int = 0) -> list[dict[str, str]]:
    """The blind adjudication prompt for one row; deterministic in (seed, row key)."""
    rng = random.Random(f"{seed}:{row_key(row)}")
    subject = window.get("subject") or row.get("subject") or {}
    kind = str(subject.get("kind") or row.get("subject_kind") or "unknown")
    killable = bool(subject.get("killable"))
    triggers = [t for t in (row.get("triggers") or window.get("triggers") or []) if isinstance(t, Mapping)]
    triggers = sorted(triggers, key=lambda t: json.dumps(t, sort_keys=True, default=str))
    rng.shuffle(triggers)
    actions = list(ls.ACTIONS)
    rng.shuffle(actions)
    events = [e for e in window.get("events") or [] if isinstance(e, Mapping)]
    shown = events[-WINDOW_EVENTS:]

    lines = [
        "## Subject",
        f"- kind: {redact_sources(kind)}",
        f"- owner: {subject.get('owner') or 'unknown'}",
        f"- can be stopped by the supervisor: {'yes' if killable else 'no'}",
        f"- has a nudge channel: {'yes' if kind in ls.NUDGEABLE_KINDS else 'no'}",
        f"- actor transcript available: {'yes' if window.get('actor_inputs_available', True) else 'no'}",
        "",
        "## Monitors that fired (score = value / threshold)",
    ]
    for hit in triggers:
        steps = ", ".join(str(s) for s in hit.get("evidence_steps") or []) or "none"
        lines.append(f"- {hit.get('trigger')} ({hit.get('kind')}, score {hit.get('score')}): "
                     f"{_blind(hit.get('detail', ''), DETAIL_CHARS)} [events: {steps}]")
    if not triggers:
        lines.append("- (none)")
    lines += ["", "## Metrics", *_metrics_text(window.get("metrics") or {}), ""]
    omitted = len(events) - len(shown)
    lines.append(f"## Recent events ({len(shown)} shown" + (f", {omitted} earlier omitted)" if omitted else ")"))
    for event in shown:
        at = event.get("at") or "no time"
        lines += [f"[event {event.get('index')}] {at} {redact_sources(str(event.get('source') or ''))}",
                  _blind(event.get("text", ""), EVENT_CHARS)]
    if not shown:
        lines.append("(no events)")
    lines += ["", "## Actions"]
    lines += [f"- {action}: {MEANINGS[action]}" for action in actions]
    lines += [
        "",
        ("Stopping (either kill action) is possible only if the subject can be stopped; a nudge "
         "only if it has a nudge channel. If the best action is not possible, choose "
         "alert_research."),
        "",
        "Return exactly one JSON object and nothing else:",
        ('{"action": "<one action name>", "reason": "<one line, at most '
         f'{REASON_WORDS} words>", "confidence": <number from 0 to 1>}}'),
    ]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


# --------------------------------------------------------------------------- adjudication


def parse_reply(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Strict: one JSON object (optionally in a ```json fence) with exactly the reply keys."""
    body = text.strip()
    if body.startswith("```"):
        first, _, rest = body.partition("\n")
        if first.strip() not in ("```", "```json") or not rest.rstrip().endswith("```"):
            return None, ["unterminated code fence"]
        body = rest.rstrip()[:-3].strip()
    try:
        form = json.loads(body)
    except ValueError as exc:
        return None, [f"not one JSON object: {exc}"]
    if not isinstance(form, dict):
        return None, ["reply is not a JSON object"]
    errors = []
    if set(form) != REPLY_KEYS:
        errors.append(f"keys must be exactly {sorted(REPLY_KEYS)}, got {sorted(form)}")
    if form.get("action") not in ls.ACTIONS:
        errors.append(f"action must be one of {list(ls.ACTIONS)}")
    reason = form.get("reason")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason.strip():
        errors.append("reason must be one non-empty line")
    elif len(reason.split()) > REASON_WORDS:
        errors.append(f"reason has {len(reason.split())} words; the limit is {REASON_WORDS}")
    confidence = form.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        errors.append("confidence must be a number from 0 to 1")
    if errors:
        return None, errors
    return {"action": form["action"], "reason": reason.strip() if isinstance(reason, str) else "",
            "confidence": float(confidence)}, []


def _base(row: Mapping[str, Any], *, model: str, seed: int) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA, "prompt_version": PROMPT_VERSION, "key": row_key(row),
        "stratum": list(stratum(row)), "decided_action": row["action"],
        "label_action": label_action(row), "adjudicator_model": model, "seed": seed,
    }


def adjudicate(row: Mapping[str, Any], observation_window: Mapping[str, Any], *, transport: Transport,
               model: str = ADJUDICATOR_MODEL, seed: int = 0) -> dict[str, Any]:
    """One blind adjudication record (``status`` ``ok`` or ``invalid``).

    Raises ``TransportError`` when the call fails or another model served it, and
    ``ValueError`` for a non-subscription route.
    """
    require_subscription_route(model)
    messages = build_messages(row, observation_window, seed=seed)
    record = _base(row, model=model, seed=seed)
    record["prompt_sha256"] = hashlib.sha256(json.dumps(messages).encode()).hexdigest()
    completion = transport.complete(model, messages, max_tokens=MAX_OUTPUT_TOKENS)
    if completion.served_model != model:
        raise TransportError(f"served by {completion.served_model}, not {model}", quota=True)
    record |= {"served_model": completion.served_model, "input_tokens": completion.input_tokens,
               "output_tokens": completion.output_tokens,
               "latency_ms": round(completion.latency_seconds * 1000)}
    form, errors = parse_reply(completion.text)
    if form is None:
        return record | {"status": "invalid", "errors": errors,
                         "reply_excerpt": completion.text[:REPLY_EXCERPT_CHARS]}
    return record | {"status": "ok", "adjudicated_action": form["action"], "reason": form["reason"],
                     "confidence": form["confidence"]}


# --------------------------------------------------------------------------- scoring


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Cohen's kappa of two raters over ``pairs``; None when empty or chance agreement is 1."""
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in left) / (n * n)
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1 - expected), 4)


def score(adjudicated: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Supervisor decision vs adjudicated action (per-action precision/recall), adjudicator
    vs replay label (agreement, kappa) and false stops. Only ``status == ok`` rows count."""
    records = list(adjudicated)
    ok = [r for r in records if r.get("status") == "ok"]
    per_action: dict[str, dict[str, Any]] = {}
    for action in ls.ACTIONS:
        tp = sum(r["decided_action"] == action and r["adjudicated_action"] == action for r in ok)
        decided = sum(r["decided_action"] == action for r in ok)
        truth = sum(r["adjudicated_action"] == action for r in ok)
        per_action[action] = {"decided": decided, "adjudicated": truth, "tp": tp, "fp": decided - tp,
                              "fn": truth - tp, "precision": _ratio(tp, decided), "recall": _ratio(tp, truth)}
    supervisor = [(str(r["decided_action"]), str(r["adjudicated_action"])) for r in ok]
    labelled = [(str(r["label_action"]), str(r["adjudicated_action"])) for r in ok
                if r.get("label_action") in ls.ACTIONS]
    false_stops = [
        {"key": r["key"], "decided_action": r["decided_action"], "adjudicated_action": r["adjudicated_action"],
         "confidence": r.get("confidence"), "reason": r.get("reason")}
        for r in ok if r["decided_action"] in ls.KILL_ACTIONS and r["adjudicated_action"] not in ls.KILL_ACTIONS
    ]
    return {
        "counts": dict(sorted(Counter(str(r.get("status")) for r in records).items())),
        "supervisor_vs_adjudicator": {"n": len(supervisor), "agreement": _ratio(sum(a == b for a, b in supervisor),
                                                                                len(supervisor)),
                                      "kappa": kappa(supervisor), "per_action": per_action},
        "adjudicator_vs_label": {"n": len(labelled), "agreement": _ratio(sum(a == b for a, b in labelled),
                                                                         len(labelled)),
                                 "kappa": kappa(labelled)},
        "false_stops": sorted(false_stops, key=lambda f: f["key"]),
    }


# --------------------------------------------------------------------------- CLI


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.expanduser().read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _append(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _latest(records: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        latest[str(record["key"])] = record
    return latest


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_supervisor_adjudicate", description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=Path, required=True, help="decision rows JSONL (replay rows or decisions-v1)")
    parser.add_argument("--windows", type=Path, required=True, help="observation windows JSONL, one per row key")
    parser.add_argument("--out", type=Path, required=True,
                        help="adjudication records JSONL (appended; finished keys are skipped on rerun)")
    parser.add_argument("--model", default=ADJUDICATOR_MODEL)
    parser.add_argument("--limit", type=int, default=DEFAULT_TOTAL, help="sample size cap")
    parser.add_argument("--per-cell", type=int, default=DEFAULT_PER_CELL)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--score-out", type=Path, help="score JSON (default: <out stem>-score.json)")
    args = parser.parse_args(argv)
    require_subscription_route(args.model)
    out = args.out.expanduser()
    windows = {str(w["key"]): w for w in read_jsonl(args.windows)}
    picked = sample(read_jsonl(args.rows), per_cell=args.per_cell, total=args.limit, seed=args.seed)
    done = {k for k, r in _latest(read_jsonl(out) if out.is_file() else []).items()
            if r.get("status") in ("ok", "invalid", "no_window")}
    owned = transport is None
    client: Transport = GatewayTransport() if transport is None else transport
    stopped: str | None = None
    try:
        for row in picked:
            key = row_key(row)
            if key in done:
                continue
            window = windows.get(key)
            if window is None:
                _append(out, _base(row, model=args.model, seed=args.seed) | {"status": "no_window"})
                continue
            try:
                record = adjudicate(row, window, transport=client, model=args.model, seed=args.seed)
            except TransportError as exc:
                _append(out, _base(row, model=args.model, seed=args.seed) | {"status": "error", "error": str(exc)})
                if exc.quota:
                    stopped = str(exc)
                    break
                continue
            _append(out, record)
    finally:
        if owned and isinstance(client, GatewayTransport):
            client.close()
    records = list(_latest(read_jsonl(out) if out.is_file() else []).values())
    table = score(records)
    (args.score_out or out.with_name(f"{out.stem}-score.json")).write_text(
        json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tokens = {k: sum(int(r.get(k) or 0) for r in records) for k in ("input_tokens", "output_tokens")}
    served = sorted({str(r["served_model"]) for r in records if r.get("served_model")})
    print(json.dumps({"sampled": len(picked), "counts": table["counts"], "served_models": served, **tokens,
                      "false_stops": len(table["false_stops"]), "stopped": stopped}, sort_keys=True))
    if stopped:
        print(f"stopped: {stopped}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
