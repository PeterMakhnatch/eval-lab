"""Plain-language, offline views of the checked insight ledger."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evallab.insights.models import InsightRecord, PassRecord, PredictionRecord
from evallab.insights.store import InsightStore


def plain_count(k: int, n: int) -> str:
    return f"{k} of {n} ({k / n:.0%})" if n else "0 of 0 (not yet measured)"


def _short(text: str) -> str:
    return " ".join(text.split())[:220]


def _served(record: PassRecord) -> str:
    return ", ".join(sorted(record.served)) or "not attested"


def _time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _display_time(value: str | None) -> str:
    if value is None:
        return "not recorded"
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return "not recorded"
    return _time(timestamp).astimezone(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %I:%M %p ET"
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
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


def _card_html(
    store: InsightStore, item: InsightRecord, position: int,
    predictions: list[PredictionRecord],
) -> str:
    card = item.card
    evidence: list[str] = []
    for reference in card.evidence:
        claim = store.get_claim(reference.claim_id)
        text = _short(claim.claim.statement) if claim else "Claim unavailable"
        evidence.append(f"{reference.claim_id}: {text} ({reference.role})")
    verdict = item.referee
    referee = (
        f"{verdict.verdict} · importance {verdict.importance} of 5 · "
        f"non-obviousness {verdict.non_obviousness} of 5"
        if verdict else "Not reviewed yet"
    )
    body = (
        f"<article class='insight'><h3>{item.rank or position}. {html.escape(card.title)}</h3>"
        f"<p class='muted'>{html.escape(item.insight_id)} · {html.escape(item.lens)} · "
        f"{html.escape(card.kind)}</p><p>{html.escape(card.statement)}</p><h4>Evidence</h4>"
        + _list_html(evidence)
        + f"<p>Referee: <span class='chip'>{html.escape(referee)}</span></p>"
    )
    if verdict:
        body += f"<p>{html.escape(verdict.notes)}</p>"
    attached = [
        f"{prediction.prediction_id}: {prediction.spec.statement} — {prediction.status}"
        for prediction in predictions if prediction.insight_id == item.insight_id
    ]
    if not attached:
        attached = [f"{prediction.statement} — not yet tracked" for prediction in card.predictions]
    body += "<h4>Predictions</h4>" + _list_html(attached or ["None proposed."])
    proposal = card.proposed_experiment
    if proposal:
        cost = f"${proposal.est_cost_usd:g}" if proposal.est_cost_usd is not None else "not estimated"
        approval = "yes" if proposal.needs_approval else "no"
        body += (
            "<h4>Proposed experiment</h4>"
            f"<p>{html.escape(proposal.hypothesis)}</p>"
            f"<p>Estimated cost: {html.escape(cost)}. Basis: {html.escape(proposal.cost_basis)}. "
            f"Needs Peter's approval: {approval}.</p>"
        )
    else:
        body += "<p>Proposed experiment: none.</p>"
    review = store.latest_review(item.insight_id)
    human = review.status if review else "pending"
    body += f"<p>Human review: <span class='chip'>{html.escape(human)}</span>"
    if review and review.note:
        body += f" — {html.escape(review.note)}"
    return body + "</p></article>"


def render_board(
    store: InsightStore,
    out_dir: Path,
    *,
    now: datetime,
    top_n: int,
) -> list[Path]:
    """Write board.md, a fully escaped standalone HTML view, and status.json."""
    if top_n < 1:
        raise ValueError("top_n must be positive")
    now = _time(now)
    cutoff = now - timedelta(days=1)
    insights = store.list_insights()
    predictions = store.list_predictions()
    passes = store.list_passes()
    by_id = {item.insight_id: item for item in insights}
    recent = [
        item for item in passes
        if item.status == "running"
        or cutoff <= _time(item.finished_at or item.started_at or item.created_at) <= now
    ]
    running = [item for item in passes if item.status == "running"]
    failures = [item for item in recent
                if item.status in {"failed", "timed_out", "invalid_output"}]
    ticks = store.query(
        "SELECT at FROM events WHERE kind LIKE '%tick%' ORDER BY at DESC LIMIT 1"
    )
    last_tick = str(ticks[0]["at"]) if ticks else store.get_meta("last_tick")
    updated = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
    lines = [
        "# Insight board", "", f"Updated {updated}.", "",
        "## Engine health", "",
        f"Last tick: {_display_time(last_tick)}. Running passes: {len(running)}. "
        f"Failed passes in the last 24 hours: {len(failures)}.", "",
        "## Top insights", "",
    ]
    eligible = [item for item in insights
                if item.status in {"checked", "refereed", "needs_revision"}
                and item.check.status != "fail"
                and item.duplicate_of is None and item.superseded_by is None]
    ranked = sorted(eligible, key=lambda item: (
        item.rank if item.rank is not None else 10**9,
        -(item.score or 0), item.insight_id,
    ))[:top_n]
    for position, item in enumerate(ranked, 1):
        card = item.card
        lines.extend([
            f"### {item.rank or position}. {card.title}", "",
            f"{item.insight_id} · {item.lens} · {card.kind}", "", card.statement, "",
            "Evidence:",
        ])
        for evidence in card.evidence:
            claim = store.get_claim(evidence.claim_id)
            statement = "Claim unavailable" if claim is None else _short(claim.claim.statement)
            lines.append(f"- {evidence.claim_id}: {statement} ({evidence.role})")
        verdict = item.referee
        lines.append(
            f"Referee: {verdict.verdict}; importance {verdict.importance} of 5; "
            f"non-obviousness {verdict.non_obviousness} of 5. {verdict.notes}"
            if verdict else "Referee: not reviewed yet."
        )
        lines.append("Predictions:")
        attached = [p for p in predictions if p.insight_id == item.insight_id]
        if attached:
            lines.extend(f"- {p.prediction_id}: {p.spec.statement} — {p.status}" for p in attached)
        else:
            lines.extend(f"- {p.statement} — not yet tracked" for p in card.predictions)
            if not card.predictions:
                lines.append("- None proposed.")
        proposal = card.proposed_experiment
        if proposal:
            cost = (f"${proposal.est_cost_usd:g}" if proposal.est_cost_usd is not None
                    else "not estimated")
            approval = "yes" if proposal.needs_approval else "no"
            lines.extend([
                f"Proposed experiment: {proposal.hypothesis}",
                f"Estimated cost: {cost}. Basis: {proposal.cost_basis}. "
                f"Needs Peter's approval: {approval}.",
            ])
        else:
            lines.append("Proposed experiment: none.")
        review = store.latest_review(item.insight_id)
        lines.extend([
            f"Human review: {review.status if review else 'pending'}"
            + (f" — {review.note}" if review and review.note else ""), "",
        ])
    if not ranked:
        lines.extend(["No checked insights yet.", ""])
    lines.extend(["## Challenges to check", ""])
    severity_order = {"blocker": 0, "major": 1, "minor": 2}
    challenges = [item for item in eligible if item.lens == "skeptic" and item.card.challenge]
    challenges.sort(key=lambda item: (
        severity_order[item.card.challenge.severity] if item.card.challenge else 3,
        item.insight_id,
    ))
    for item in challenges[:top_n]:
        challenge = item.card.challenge
        if challenge is not None:
            lines.append(
                f"- {challenge.severity}: {item.card.title} ({item.insight_id}; "
                f"{', '.join(challenge.target_claim_ids)}). Check: {challenge.check} "
                f"Decision at risk: {challenge.decision_at_risk}"
            )
    if not challenges:
        lines.append("No checked challenges waiting.")
    lines.extend(["", "## Contradictions and drifts", ""])
    signals = store.list_signals(status="open", limit=top_n)
    for signal in signals:
        lines.append(
            f"- {signal.title} ({signal.kind}; {signal.signal_id}; "
            f"{', '.join(signal.claim_ids)})."
        )
    if not signals:
        lines.append("No open signals.")
    prediction_groups: dict[str, list[PredictionRecord]] = defaultdict(list)
    for prediction in predictions:
        insight = by_id.get(prediction.insight_id)
        prediction_groups[insight.lens if insight else "unknown"].append(prediction)
    prediction_scores: dict[str, dict[str, int]] = {}
    lines.extend(["", "## Predictions scoreboard", ""])
    for lens, group in sorted(prediction_groups.items()):
        counts = {status: sum(p.status == status for p in group)
                  for status in ("open", "hit", "miss", "untestable")}
        prediction_scores[lens] = counts
        lines.append(
            f"- {lens}: open {counts['open']}; hit {counts['hit']}; miss {counts['miss']}; "
            f"untestable {counts['untestable']}. Hits among resolved: "
            f"{plain_count(counts['hit'], counts['hit'] + counts['miss'])}."
        )
    if not prediction_groups:
        lines.append("No tracked predictions yet.")
    groups: dict[tuple[str, str], list[InsightRecord]] = defaultdict(list)
    for item in insights:
        groups[(item.lens, item.model_served or "not attested")].append(item)
    lens_scores: list[dict[str, object]] = []
    lines.extend(["", "## Lens scoreboard", ""])
    for (lens, model), group in sorted(groups.items()):
        checked = sum(item.check.status == "pass" for item in group)
        reviewed = [item for item in group if item.referee is not None]
        accepted = sum(item.referee is not None and item.referee.verdict == "accept"
                       for item in reviewed)
        ids = {item.insight_id for item in group}
        resolved = [p for p in predictions if p.insight_id in ids and p.status in {"hit", "miss"}]
        hits = sum(p.status == "hit" for p in resolved)
        lens_scores.append({
            "lens": lens, "served_model": model, "cards": len(group),
            "checks_passed": checked, "refereed": len(reviewed), "referee_accepted": accepted,
            "prediction_hits": hits, "predictions_resolved": len(resolved),
        })
        lines.append(
            f"- {lens} / {model}: {len(group)} cards; code checks passed "
            f"{plain_count(checked, len(group))}; referee accepted "
            f"{plain_count(accepted, len(reviewed))}; prediction hits "
            f"{plain_count(hits, len(resolved))}."
        )
    if not groups:
        lines.append("No cards yet.")
    lines.extend(["", "## Recent activity", "", "Passes active in the last 24 hours:", ""])
    activity: list[dict[str, object]] = []
    for record in recent:
        start = record.started_at or record.created_at
        minutes = max(0, (_time(record.finished_at or now) - _time(start)).total_seconds() / 60)
        activity.append({
            "pass_id": record.pass_id, "lens": record.lens, "status": record.status,
            "served": record.served, "minutes": round(minutes, 1),
        })
        lines.append(
            f"- {record.lens}: {record.status}; served model {_served(record)}; "
            f"{minutes:.1f} minutes ({record.pass_id})."
        )
    if not recent:
        lines.append("No recent passes.")
    lines.extend([
        "", "---", "",
        "Code checks evidence references, quoted text, supported numbers, receipt recipes and "
        "statistical calculations. Models judge meaning, novelty, importance and proposed "
        "explanations. A model's approval is not proof; a human review is separate.", "",
    ])
    markdown = "\n".join(lines)
    prediction_rows = [
        [
            lens, str(counts["open"]), str(counts["hit"]), str(counts["miss"]),
            str(counts["untestable"]), plain_count(counts["hit"], counts["hit"] + counts["miss"]),
        ]
        for lens, counts in sorted(prediction_scores.items())
    ]
    lens_rows: list[list[str]] = []
    for (lens, model), group in sorted(groups.items()):
        reviewed = [item for item in group if item.referee is not None]
        accepted = sum(item.referee is not None and item.referee.verdict == "accept"
                       for item in reviewed)
        identities = {item.insight_id for item in group}
        resolved = [p for p in predictions
                    if p.insight_id in identities and p.status in {"hit", "miss"}]
        lens_rows.append([
            lens, model, str(len(group)),
            plain_count(sum(item.check.status == "pass" for item in group), len(group)),
            plain_count(accepted, len(reviewed)),
            plain_count(sum(p.status == "hit" for p in resolved), len(resolved)),
        ])
    activity_rows = [
        [record.lens, record.status, _served(record), str(entry["minutes"])]
        for record, entry in zip(recent, activity, strict=True)
    ]
    challenge_text = [
        f"{challenge.severity}: {item.card.title} ({item.insight_id}; "
        f"{', '.join(challenge.target_claim_ids)}). Check: {challenge.check} "
        f"Decision at risk: {challenge.decision_at_risk}"
        for item in challenges[:top_n] if (challenge := item.card.challenge) is not None
    ]
    signal_text = [
        f"{signal.title} ({signal.kind}; {signal.signal_id}; {', '.join(signal.claim_ids)})"
        for signal in signals
    ]
    sections = [
        "<h2>Engine health</h2><p>"
        + html.escape(
            f"Last tick: {_display_time(last_tick)}. Running passes: {len(running)}. "
            f"Failed passes in the last 24 hours: {len(failures)}."
        ) + "</p>",
        "<h2>Top insights</h2>" + (
            "".join(_card_html(store, item, index, predictions)
                    for index, item in enumerate(ranked, 1))
            or "<p>No checked insights yet.</p>"
        ),
        "<h2>Challenges to check</h2>"
        + _list_html(challenge_text or ["No checked challenges waiting."]),
        "<h2>Contradictions and drifts</h2>"
        + _list_html(signal_text or ["No open signals."]),
        "<h2>Predictions scoreboard</h2>"
        + _table(["Lens", "Open", "Hit", "Miss", "Untestable", "Hits among resolved"], prediction_rows),
        "<h2>Lens scoreboard</h2>"
        + _table(["Lens", "Served model", "Cards", "Code checks passed",
                  "Referee accepted", "Prediction hits"], lens_rows),
        "<h2>Recent activity</h2><p>Passes active in the last 24 hours.</p>"
        + _table(["Lens", "Status", "Served model", "Minutes"], activity_rows),
        "<hr><p class='muted'>" + html.escape(lines[-2]) + "</p>",
    ]
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Insight board</title><style>'
        'body{max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18212b;'
        'background:#fafbfc;font:16px/1.55 system-ui,sans-serif;overflow-wrap:anywhere}'
        'h2{margin-top:2rem}.insight{border:1px solid #ccd4df;border-radius:10px;'
        'padding:1rem 1.3rem;margin:1rem 0;background:white}h3,h4{margin-bottom:.4rem}'
        '.chip{display:inline-block;background:#e5edf8;padding:.15rem .5rem;border-radius:6px}'
        '.muted{color:#536170}.table-wrap{overflow-x:auto}table{border-collapse:collapse;'
        'width:100%;background:white}th,td{text-align:left;border:1px solid #dbe1e8;'
        'padding:.5rem .7rem}th{background:#edf2f7}'
        '</style></head><body><h1>Insight board</h1><p>Updated '
        + html.escape(updated) + ".</p>" + "".join(sections) + "</body></html>\n"
    )
    status = {
        "updated_at": now.isoformat(), "last_tick": last_tick,
        "running_passes": len(running), "failures_last_24h": len(failures),
        "top_insight_ids": [item.insight_id for item in ranked],
        "open_signal_ids": [item.signal_id for item in signals],
        "prediction_scoreboard": prediction_scores, "lens_scoreboard": lens_scores,
        "recent_activity": activity,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / name for name in ("board.md", "board.html", "status.json")]
    paths[0].write_text(markdown, encoding="utf-8")
    paths[1].write_text(document, encoding="utf-8")
    paths[2].write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths
