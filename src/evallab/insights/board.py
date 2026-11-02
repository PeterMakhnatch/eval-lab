"""Plain-language, offline views of the checked insight ledger."""

from __future__ import annotations

import html
import json
import re
import shlex
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evallab.insights.models import HumanReview, InsightRecord, PassRecord, PredictionRecord
from evallab.insights.store import InsightStore

LANES = {
    "refereed_accepted": "Refereed: accepted",
    "awaiting_referee": "Awaiting referee",
    "needs_revision": "Needs revision",
    "referee_rejected": "Rejected by referee",
    "check_failed": "Failed code checks — quarantine",
    "human_accepted": "Human-reviewed: accepted",
    "human_rejected": "Human-reviewed: rejected",
    "human_needs_evidence": "Human-reviewed: needs_evidence",
    "archived": "Duplicates / superseded",
}
PROPOSAL_NOTICE = "Proposal only; execution not authorized. execution_authorized: false."


def plain_count(k: int, n: int) -> str:
    return f"{k} of {n} ({k / n:.0%})" if n else "0 of 0 (not yet measured)"


def _wilson_text(value: object) -> str:
    if not isinstance(value, dict):
        return "interval unavailable"
    low, high = value.get("low"), value.get("high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return "interval unavailable"
    return f"Wilson 95% [{low:.2f}, {high:.2f}]"


def _calibration_text(store: InsightStore, now: datetime) -> tuple[str, dict[str, object]]:
    rates = store.calibration_rates(now=now)
    twin = rates["twin"]
    original = rates["original"]
    if not isinstance(twin, dict) or not isinstance(original, dict):
        return "Referee calibration unavailable.", rates
    tk, tn = int(twin["k"]), int(twin["n"])
    ok, on = int(original["k"]), int(original["n"])
    text = (
        f"Referee calibration, last 30 days: referee caught {tk} of {tn} planted flawed cards "
        f"({_wilson_text(twin['wilson'])}); accepted {ok} of {on} original controls "
        f"({_wilson_text(original['wilson'])})."
    )
    if tn == 0:
        text += " Referee uncalibrated: no planted cards graded yet."
    return text, rates


def _md_line(text: str) -> str:
    """Keep untrusted ledger text on one plain, non-structural Markdown line."""
    value = re.sub(r"\s+", " ", text).strip()
    return ("\\" + value) if re.match(r"^(?:[#>\-|]|`{3})", value) else value


def _short(text: str) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= 220 else compact[:219] + "…"


def _served(record: PassRecord) -> str:
    return ", ".join(sorted(record.served)) or "not attested"


def _time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _time(datetime.fromisoformat(value))
    except ValueError:
        return None


def _display_time(value: str | None) -> str:
    timestamp = _timestamp(value)
    return timestamp.astimezone(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %I:%M:%S %p ET"
    ) if timestamp else "not recorded"


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{html.escape(empty)}</p>"
    heading = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return (
        f"<div class='table-wrap'><table><thead><tr>{heading}</tr></thead><tbody>{body}"
        "</tbody></table></div>"
    )


def _list_html(values: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in values) + "</ul>"


def _anchor(identity: str) -> str:
    return "item-" + identity.encode().hex()


def _state_option(store: InsightStore) -> str:
    # Explicitly target this ledger, including nondefault and copied snapshot roots.
    return " --state-dir " + shlex.quote(str(store.path.resolve().parent))


def _show_command(store: InsightStore, identity: str) -> str:
    return f"evallab insights show {shlex.quote(identity)}{_state_option(store)}"


def _review_commands(store: InsightStore, identity: str) -> list[str]:
    return [
        f'evallab insights review {shlex.quote(identity)} {status} --by "Peter Makhnatch"'
        + _state_option(store)
        for status in ("accepted", "rejected", "needs_evidence")
    ]


def _lane(item: InsightRecord, review: HumanReview | None) -> str:
    # A human acceptance cannot override a failing evidence check or revive an archive.
    if item.check.status == "fail" or item.status == "check_failed":
        return "check_failed"
    if item.duplicate_of or item.superseded_by or item.status in {"duplicate", "superseded"}:
        return "archived"
    if review and review.status != "pending":
        return "human_" + review.status
    if item.referee:
        return {"accept": "refereed_accepted", "revise": "needs_revision",
                "reject": "referee_rejected"}[item.referee.verdict]
    if item.status == "needs_revision":
        return "needs_revision"
    if item.status == "rejected":
        return "referee_rejected"
    return "awaiting_referee"


def _card_text(
    store: InsightStore, item: InsightRecord, review: HumanReview | None,
    predictions: list[PredictionRecord],
) -> list[str]:
    card = item.card
    verdict = item.referee
    lines = [
        f"Lens: {item.lens}. Author (attested): {item.model_served or 'not attested'}.",
        f"Code checks: {item.check.status}.",
        *[f"Code-check note: {note}" for note in item.check.notes],
        f"Referee: {verdict.verdict if verdict else 'awaiting review'}; "
        f"model (attested): {item.referee_model or 'not attested'}.",
    ]
    if item.check.status in {"warn", "fail"} and not item.check.notes:
        lines.append("Code-check note: no reason recorded.")
    if verdict:
        lines.extend([
            f"Importance {verdict.importance} of 5; non-obviousness "
            f"{verdict.non_obviousness} of 5. {verdict.notes}",
        ])
    lines.append(
        f"Human review: {review.status}; by {review.by}; {_display_time(review.at.isoformat())}."
        + (f" {review.note}" if review.note else "") if review else "Human review: pending."
    )
    if item.duplicate_of:
        lines.append(f"Duplicate of: {item.duplicate_of}.")
    if item.superseded_by:
        lines.append(f"Superseded by: {item.superseded_by}.")
    lines.extend([f"Observations: {'; '.join(card.observations)}",
                  f"Interpretation: {card.interpretation}",
                  f"Why not obvious: {card.why_not_obvious}",
                  f"Alternatives: {'; '.join(card.alternatives)}"])
    if card.challenge:
        challenge = card.challenge
        lines.append(f"Challenge ({challenge.severity}): {challenge.check} "
                     f"Decision at risk: {challenge.decision_at_risk}. "
                     f"Target claims: {', '.join(challenge.target_claim_ids)}.")
    attached = [p for p in predictions if p.insight_id == item.insight_id]
    lines.extend(f"Prediction {p.prediction_id}: {p.spec.statement} — {p.status}" for p in attached)
    if not attached:
        lines.extend(f"Prediction: {p.statement} — not yet tracked" for p in card.predictions)
    if not attached and not card.predictions:
        lines.append("Predictions: none proposed.")
    proposal = card.proposed_experiment
    if proposal:
        cost = f"${proposal.est_cost_usd:g}" if proposal.est_cost_usd is not None else "not estimated"
        lines.extend([
            PROPOSAL_NOTICE,
            f"Proposed experiment: {proposal.hypothesis}",
            f"Estimated cost: {cost}. Basis: {proposal.cost_basis}.",
            "Paid/cloud approval requirement in proposal: "
            + ("required" if proposal.needs_approval else "not flagged")
            + "; this is not an execution approval.",
        ])
    return lines


def _card_views(
    store: InsightStore, item: InsightRecord, review: HumanReview | None,
    predictions: list[PredictionRecord],
) -> tuple[list[str], str]:
    card = item.card
    identity = item.insight_id
    rank = f"rank {item.rank}" if item.rank is not None else "unranked"
    referee = item.referee.verdict if item.referee else "awaiting review"
    metadata = (
        f"{identity} · {item.lens} · {card.kind} · {rank} · "
        f"author (attested): {item.model_served or 'not attested'}"
    )
    qc = (f"Code: {item.check.status} · Referee: {referee} · "
          f"referee model: {item.referee_model or 'not attested'} · "
          f"Human: {review.status if review else 'pending'}")
    detail = _card_text(store, item, review, predictions)
    commands = _review_commands(store, identity)
    evidence_md: list[str] = []
    evidence_html: list[str] = []
    for reference in card.evidence:
        claim = store.get_claim(reference.claim_id)
        text = claim.claim.statement if claim else "Claim unavailable"
        command = _show_command(store, reference.claim_id)
        evidence_md.extend([
            _md_line(f"- {reference.claim_id}: {text} ({reference.role})"),
            _md_line(f"  {command}"),
        ])
        evidence_html.append(
            f"<li><a href='#{_anchor(reference.claim_id)}'>{html.escape(reference.claim_id)}</a>: "
            f"{html.escape(text)} ({html.escape(reference.role)})</li>"
        )
    markdown = [f"### {_md_line(card.title)}", "", _md_line(metadata), "",
                _md_line(card.statement), "", _md_line(qc),
                *(_md_line(line) for line in detail), "", "Evidence:", *evidence_md,
                "", "Review commands:", *(_md_line(command) for command in commands), ""]
    # All ledger/model/source strings enter HTML only through html.escape.
    body = (
        f"<article class='insight' id='{_anchor(identity)}'><h3>{html.escape(card.title)}</h3>"
        f"<p class='muted metadata'>{html.escape(metadata)}</p>"
        f"<p class='takeaway'>{html.escape(_short(card.statement))}</p>"
        f"<p class='qc {html.escape(item.check.status)}'>{html.escape(qc)}</p>"
    )
    if item.check.status in {"warn", "fail"}:
        body += "<div class='check-notes'>" + _list_html(
            item.check.notes or ["No code-check reason recorded."]
        ) + "</div>"
    if card.proposed_experiment:
        body += f"<p class='authorization'>{html.escape(PROPOSAL_NOTICE)}</p>"
    body += (
        "<p class='citations'>Cited claims: "
        + ", ".join(f"<a href='#{_anchor(ref.claim_id)}'>{html.escape(ref.claim_id)}</a>"
                    for ref in card.evidence)
        + "</p><details><summary>Evidence, reasoning, referee notes and review commands</summary>"
        f"<p>{html.escape(card.statement)}</p>" + _list_html(detail)
        + "<h4>Evidence</h4><ul>" + "".join(evidence_html) + "</ul>"
        + "<h4>Review commands</h4>"
        + "".join(f"<p><code>{html.escape(command)}</code></p>" for command in commands)
        + "<p>Accepting an insight does not authorize its proposed experiment.</p>"
        + "</details></article>"
    )
    return markdown, body


def _tick_health(store: InsightStore, now: datetime) -> dict[str, object]:
    raw = store.get_meta("last_tick")
    report: dict[str, object] = {}
    errors: list[str] = []
    if raw:
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, dict):
            report = decoded
        elif not _timestamp(raw):
            errors.append("Stored last_tick metadata is not a valid tick report or timestamp.")
    timestamp = _timestamp(report.get("finished_at")) or _timestamp(report.get("started_at"))
    if timestamp is None:
        timestamp = _timestamp(raw)
    if timestamp is None:
        ticks = store.query("SELECT at FROM events WHERE kind LIKE '%tick%' ORDER BY at DESC LIMIT 1")
        timestamp = _timestamp(ticks[0]["at"]) if ticks else None
    stored_errors = report.get("errors", [])
    if isinstance(stored_errors, list):
        errors.extend(str(error) for error in stored_errors)
    age = max(0, (now - timestamp).total_seconds()) if timestamp else None
    return {"last_tick": timestamp.isoformat() if timestamp else None,
            "last_tick_report": report, "last_tick_age_seconds": age, "tick_errors": errors}


def render_board(
    store: InsightStore, out_dir: Path, *, now: datetime, top_n: int,
) -> list[Path]:
    """Write board.md, a fully escaped standalone HTML view, and status.json.

    Each QC lane and the signals list are capped independently at top_n; totals
    always describe the complete ledger. Human reviews override the machine queue,
    but never remove failed code checks from quarantine.
    """
    if top_n < 1:
        raise ValueError("top_n must be positive")
    now = _time(now)
    insights = store.list_insights()
    predictions = store.list_predictions()
    passes = store.list_passes()
    reviews = {item.insight_id: store.latest_review(item.insight_id) for item in insights}
    by_id = {item.insight_id: item for item in insights}
    lanes: dict[str, list[InsightRecord]] = {key: [] for key in LANES}
    for item in insights:
        lanes[_lane(item, reviews[item.insight_id])].append(item)
    for group in lanes.values():
        group.sort(key=lambda item: (item.rank if item.rank is not None else 10**9,
                                    -(item.score or 0), -_time(item.created_at).timestamp(),
                                    item.insight_id))
    accepted = lanes["refereed_accepted"] + lanes["human_accepted"]
    newest = sorted(accepted, key=lambda item: _time(item.created_at), reverse=True)[:3]
    top = sorted(accepted, key=lambda item: (item.rank if item.rank is not None else 10**9,
                                           -(item.score or 0), item.insight_id))[:top_n]
    cutoff = now - timedelta(days=1)
    recent = [item for item in passes if item.status == "running"
              or cutoff <= _time(item.finished_at or item.started_at or item.created_at) <= now]
    running = [item for item in passes if item.status == "running"]
    failures = [item for item in recent if item.status in {"failed", "timed_out", "invalid_output"}]
    health = _tick_health(store, now)
    age = health["last_tick_age_seconds"]
    age_text = f"{age / 60:.1f} minutes old" if isinstance(age, (int, float)) else "age unknown"
    tick = health["last_tick"]
    health_text = (f"Last tick: {_display_time(tick if isinstance(tick, str) else None)} "
                   f"({age_text}). Running passes: {len(running)}. "
                   f"Failed passes in the last 24 hours: {len(failures)}.")
    errors = health["tick_errors"]
    health_notes = [str(error) for error in errors] if isinstance(errors, list) else []
    report = health["last_tick_report"]
    if isinstance(report, dict) and isinstance(report.get("waiting"), dict):
        health_notes.extend(f"{lens}: {reason}" for lens, reason in report["waiting"].items())
    health_notes.extend(
        f"read scope: {record.pass_id} has {record.scope_violations.count} violation(s): "
        + "; ".join(record.scope_violations.examples)
        for record in recent if record.scope_violations.count
    )
    calibration_text, calibration = _calibration_text(store, now)
    updated = _display_time(now.isoformat())
    state_label = f"Ledger: {store.path.resolve()}. Render time is not source freshness."
    counts_text = " · ".join(f"{title}: {len(lanes[key])}" for key, title in LANES.items())
    newest_text = [f"{item.insight_id}: {_short(item.card.title)}" for item in newest]
    lines = ["# Insight board", "", f"Rendered {updated}.", "", state_label, "",
             "## Morning decision scan", "", counts_text, "",
             "Machine referee acceptance is not human acceptance. Review accepted findings first; "
             "pending, revision and rejected cards remain separate.", "",
             "Newest accepted items (referee or human):", *[f"- {text}" for text in newest_text], ""]
    if not newest:
        lines.extend(["No accepted findings yet; awaiting referee is not a top-insight ranking.", ""])
    sections = [
        "<section class='scan'><h2>Morning decision scan</h2><p>"
        + html.escape(counts_text) + "</p><p>Review accepted findings first. "
        "Machine referee acceptance is not human acceptance.</p><h3>Newest accepted items</h3>"
        + ("<ul>" + "".join(
            f"<li><a href='#{_anchor(item.insight_id)}'>{html.escape(item.insight_id)}</a>: "
            f"{html.escape(_short(item.card.title))}</li>" for item in newest
        ) + "</ul>" if newest else "<p>No accepted findings yet; pending cards are not top insights.</p>")
        + "</section>",
    ]
    lines.extend(["## Engine health", "", health_text,
                  calibration_text,
                  f"Tick errors: {len(errors) if isinstance(errors, list) else 0}.",
                  *(_md_line(f"- {note}") for note in health_notes), ""])
    sections.append("<h2>Engine health</h2><p>" + html.escape(health_text) + "</p><p>"
                    + html.escape(calibration_text) + "</p>"
                    + f"<details><summary>Tick errors: {len(errors) if isinstance(errors, list) else 0}"
                    + " · waiting reasons</summary>"
                    + _list_html(health_notes or ["No tick errors or waiting reasons recorded."])
                    + "</details>")
    headline, summary = store.get_meta("board_headline"), store.get_meta("board_summary")
    if headline or summary:
        lines.extend(["## Synthesizer context (model-written)", "", _md_line(headline or ""),
                      _md_line(summary or ""), ""])
        sections.append("<details><summary>Synthesizer context (model-written)</summary><h3>"
                        + html.escape(headline or "") + "</h3><p>"
                        + html.escape(summary or "") + "</p></details>")
    displayed: list[InsightRecord] = []
    for key, title in LANES.items():
        group = lanes[key]
        shown = group[:top_n]
        displayed.extend(shown)
        caption = f"{title} — showing {len(shown)} of {len(group)}"
        lines.extend([f"## {caption}", ""])
        cards_html: list[str] = []
        for item in shown:
            card_lines, card_html = _card_views(store, item, reviews[item.insight_id], predictions)
            lines.extend(card_lines)
            cards_html.append(card_html)
        if not shown:
            lines.extend(["No cards in this lane.", ""])
        if len(group) > len(shown):
            lines.extend(["Lane truncated; inspect the ledger for the complete queue.", ""])
        sections.append(f"<section id='lane-{key}'><h2>{html.escape(caption)}</h2>"
                        + ("".join(cards_html) or "<p class='muted'>No cards in this lane.</p>")
                        + ("<p>Lane truncated; inspect the ledger for the complete queue.</p>"
                           if len(group) > len(shown) else "") + "</section>")
    signals = store.list_signals(status="open", limit=top_n)
    signal_total = int(store.query("SELECT COUNT(*) AS total FROM signals WHERE status = 'open'")[0]["total"])
    signal_caption = f"Open QC signals — showing {len(signals)} of {signal_total}"
    signal_text = [f"{s.kind}: {s.title} ({s.signal_id}; {', '.join(s.claim_ids)})" for s in signals]
    signal_note = ("List truncated. " if signal_total > len(signals) else "Complete open-signal list. ")
    signal_command = "evallab insights query \"SELECT * FROM v_signals WHERE status = 'open'\"" + _state_option(store)
    lines.extend([f"## {signal_caption}", "", signal_note,
                  *(_md_line(f"- {text}") for text in signal_text),
                  "Complete list command:", signal_command, ""])
    sections.append("<h2>" + html.escape(signal_caption) + "</h2><p>" + html.escape(signal_note)
                    + "</p>" + _list_html(signal_text or ["No open signals."])
                    + "<p>Complete list: <code>" + html.escape(signal_command) + "</code></p>")
    prediction_groups: dict[str, list[PredictionRecord]] = defaultdict(list)
    for prediction in predictions:
        insight = by_id.get(prediction.insight_id)
        prediction_groups[insight.lens if insight else "unknown"].append(prediction)
    prediction_scores: dict[str, dict[str, int]] = {}
    prediction_rows: list[list[str]] = []
    lines.extend(["## Predictions scoreboard", ""])
    for lens, group in sorted(prediction_groups.items()):
        counts = {status: sum(p.status == status for p in group)
                  for status in ("open", "hit", "miss", "untestable")}
        prediction_scores[lens] = counts
        hits = plain_count(counts["hit"], counts["hit"] + counts["miss"])
        lines.append(f"- {lens}: open {counts['open']}; hit {counts['hit']}; miss {counts['miss']}; "
                     f"untestable {counts['untestable']}. Hits among resolved: {hits}.")
        prediction_rows.append([lens, *[str(counts[s]) for s in ("open", "hit", "miss", "untestable")], hits])
    if not prediction_rows:
        lines.append("No tracked predictions yet.")
    sections.append("<h2>Predictions scoreboard</h2>" + _table(
        ["Lens", "Open", "Hit", "Miss", "Untestable", "Hits among resolved"], prediction_rows,
        "No tracked predictions yet."))
    groups: dict[tuple[str, str], list[InsightRecord]] = defaultdict(list)
    for item in insights:
        groups[(item.lens, item.model_served or "not attested")].append(item)
    lens_scores: list[dict[str, object]] = []
    lens_rows: list[list[str]] = []
    lines.extend(["", "## Lens scoreboard", ""])
    for (lens, model), group in sorted(groups.items()):
        checks = {status: sum(item.check.status == status for item in group)
                  for status in ("pass", "warn", "fail")}
        reviewed = [item for item in group if item.referee]
        referee_accepted = sum(item.referee is not None and item.referee.verdict == "accept" for item in group)
        human_accepted = sum(
            review is not None and review.status == "accepted"
            for item in group if (review := reviews[item.insight_id]) is not None
        )
        ids = {item.insight_id for item in group}
        resolved = [p for p in predictions if p.insight_id in ids and p.status in {"hit", "miss"}]
        hits = sum(p.status == "hit" for p in resolved)
        lens_scores.append({"lens": lens, "served_model": model, "cards": len(group),
                            "checks_passed": checks["pass"], "checks_warned": checks["warn"],
                            "checks_failed": checks["fail"], "refereed": len(reviewed),
                            "referee_accepted": referee_accepted, "human_accepted": human_accepted,
                            "prediction_hits": hits, "predictions_resolved": len(resolved)})
        check_text = f"{checks['pass']} pass / {checks['warn']} warn / {checks['fail']} fail"
        lines.append(_md_line(f"- {lens} / {model}: {len(group)} cards; code checks {check_text}; "
                     f"referee accepted {plain_count(referee_accepted, len(reviewed))}; "
                     f"human accepted {human_accepted}; prediction hits {plain_count(hits, len(resolved))}."))
        lens_rows.append([lens, model, str(len(group)), check_text,
                          plain_count(referee_accepted, len(reviewed)), str(human_accepted),
                          plain_count(hits, len(resolved))])
    if not lens_rows:
        lines.append("No cards yet.")
    sections.append("<h2>Lens scoreboard</h2>" + _table(
        ["Lens", "Attested author", "Cards", "Code checks", "Referee accepted", "Human accepted", "Prediction hits"],
        lens_rows, "No cards yet."))
    activity: list[dict[str, object]] = []
    activity_rows: list[list[str]] = []
    lines.extend(["", "## Recent activity", "", "Passes active in the last 24 hours:", ""])
    for record in recent:
        start = record.started_at or record.created_at
        minutes = max(0, (_time(record.finished_at or now) - _time(start)).total_seconds() / 60)
        activity.append({"pass_id": record.pass_id, "lens": record.lens, "status": record.status,
                         "served": record.served, "minutes": round(minutes, 1)})
        lines.append(f"- {record.lens}: {record.status}; served model {_served(record)}; "
                     f"{minutes:.1f} minutes ({record.pass_id}).")
        activity_rows.append([record.lens, record.status, _served(record), f"{minutes:.1f}"])
    if not activity:
        lines.append("No recent passes.")
    sections.append("<h2>Recent activity</h2>" + _table(
        ["Lens", "Status", "Attested served model", "Minutes"], activity_rows, "No recent passes."))
    evidence_ids = sorted({ref.claim_id for item in displayed for ref in item.card.evidence})
    lines.extend(["", "## Evidence commands", ""])
    evidence_links: list[str] = []
    for identity in evidence_ids:
        command = _show_command(store, identity)
        lines.append(command)
        evidence_links.append(f"<li id='{_anchor(identity)}'><code>{html.escape(command)}</code></li>")
    sections.append("<details><summary>Evidence commands</summary><ul>" + "".join(evidence_links) + "</ul></details>")
    disclaimer = ("Code checks evidence references, quoted text, supported numbers, receipt recipes and "
                  "statistical calculations. Models judge meaning, novelty, importance and proposed "
                  "explanations. A model's approval is not proof; a human review is separate. "
                  "Accepting an insight never authorizes experiment execution.")
    lines.extend(["", "---", "", disclaimer, ""])
    sections.append("<hr><p class='muted'>" + html.escape(disclaimer) + "</p>")
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Insight board</title><style>'
        'body{max-width:1120px;margin:2rem auto;padding:0 1.2rem;color:#18212b;'
        'background:#fafbfc;font:15px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}'
        'h1{margin-bottom:.3rem}h2{margin-top:1.8rem}h3{margin:.2rem 0 .6rem}'
        '.scan{background:#eaf2fb;border:1px solid #b8cce3;padding:1rem 1.3rem;border-radius:10px}'
        '.insight{border:1px solid #ccd4df;border-radius:10px;padding:1rem 1.3rem;margin:1rem 0;background:white}'
        '.insight p{margin:.5rem 0}.metadata{font-size:.86rem}.qc{font-weight:600;font-size:.86rem}'
        '.warn,.check-notes{color:#784c00}.fail{color:#a32727}.check-notes{font-size:.86rem}'
        '.check-notes ul{margin:.35rem 0;padding-left:1.2rem}.authorization{border-left:3px solid #b87918;'
        'padding-left:.6rem;font-size:.9rem}.citations{font-size:.86rem}'
        'details{margin:.6rem 0}summary{cursor:pointer;color:#244c77;font-weight:600}'
        'code{font:12px/1.5 ui-monospace,monospace;background:#edf2f7;padding:.15rem .3rem;white-space:pre-wrap}'
        'a{color:#185a98}.muted{color:#536170}.table-wrap{overflow-x:auto}'
        'table{border-collapse:collapse;width:100%;background:white;font-size:.86rem}'
        'th,td{text-align:left;border:1px solid #dbe1e8;padding:.5rem .6rem}th{background:#edf2f7}'
        '</style></head><body><h1>Insight board</h1><p class="muted">Rendered '
        + html.escape(updated) + '.</p><p class="muted metadata">' + html.escape(state_label)
        + '</p>' + ''.join(sections) + '</body></html>\n'
    )
    status = {
        "updated_at": now.isoformat(), **health, "ledger_path": str(store.path.resolve()),
        "running_passes": len(running), "failures_last_24h": len(failures),
        "top_insight_ids": [item.insight_id for item in top],
        "newest_accepted_ids": [item.insight_id for item in newest],
        "lane_counts": {key: len(group) for key, group in lanes.items()},
        "lane_insight_ids": {key: [item.insight_id for item in group] for key, group in lanes.items()},
        "displayed_lane_insight_ids": {key: [item.insight_id for item in group[:top_n]] for key, group in lanes.items()},
        "lanes_truncated": {key: len(group) > top_n for key, group in lanes.items()},
        "quarantine_count": len(lanes["check_failed"]),
        "cards": [{"insight_id": item.insight_id, "lane": _lane(item, reviews[item.insight_id]),
                   "lens": item.lens, "model_served": item.model_served,
                   "check": item.check.model_dump(mode="json"),
                   "referee": item.referee.model_dump(mode="json") if item.referee else None,
                   "referee_model": item.referee_model,
                   "human_review": review.model_dump(mode="json")
                   if (review := reviews[item.insight_id]) is not None else None,
                   "claim_ids": [ref.claim_id for ref in item.card.evidence],
                   "execution_authorized": False if item.card.proposed_experiment else None}
                  for item in insights],
        "open_signals_total": signal_total, "displayed_open_signal_ids": [s.signal_id for s in signals],
        "signals_truncated": signal_total > len(signals),
        "prediction_scoreboard": prediction_scores, "lens_scoreboard": lens_scores,
        "recent_activity": activity,
        "referee_calibration": calibration,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / name for name in ("board.md", "board.html", "status.json")]
    paths[0].write_text("\n".join(lines), encoding="utf-8")
    paths[1].write_text(document, encoding="utf-8")
    paths[2].write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths
