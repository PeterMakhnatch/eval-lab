from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .seat_cda import TrialCdaReport

__all__ = [
    "HTML_CDA_SCRIPT",
    "aggregate_cda_reports",
    "dump_trial_cda",
    "merge_corpus_cda",
    "normalize_sender_credit",
]


def normalize_sender_credit(
    raw_by_seat: Mapping[str, float], roster: Sequence[str]
) -> dict[str, float]:
    ordered = list(dict.fromkeys(roster))
    assert all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0.0
        for v in raw_by_seat.values()
    )
    total = math.fsum(float(raw_by_seat.get(s, 0.0)) for s in ordered)
    if total <= 0.0 or not ordered:
        return dict.fromkeys(ordered, 0.0)
    shares = {s: round(float(raw_by_seat.get(s, 0.0)) / total, 6) for s in ordered}
    top = max(ordered, key=lambda s: (float(raw_by_seat.get(s, 0.0)), s))
    shares[top] = round(max(0.0, 1.0 - math.fsum(v for k, v in shares.items() if k != top)), 6)
    assert math.isclose(math.fsum(shares.values()), 1.0, abs_tol=1e-5)
    return shares


def dump_trial_cda(
    cda: TrialCdaReport, *, max_messages: int = 80, max_edges: int = 120
) -> dict[str, Any]:
    if type(max_messages) is not int or max_messages < 0:
        raise ValueError(f"max_messages must be >= 0, got {max_messages!r}")
    if type(max_edges) is not int or max_edges < 0:
        raise ValueError(f"max_edges must be >= 0, got {max_edges!r}")
    dumped = cda.model_dump(mode="json")
    full_msgs = dumped["messages"]
    full_edges = dumped["edges"]
    gi = dumped["guard_impact"]
    assert (
        gi["hash_chain_valid_trials"] + gi["hash_chain_tampered_trials"]
        == gi["hash_chain_checked_trials"]
    )
    return {
        "messages_posted": dumped["messages_posted"],
        "messages_fetched": dumped["messages_fetched"],
        "avoided_duplicate_targets": dumped["avoided_duplicate_targets"],
        "triggered_actions": dumped["triggered_actions"],
        "ignored_messages": dumped["ignored_messages"],
        "sender_causal_credit": dumped["sender_causal_credit"],
        "guard_impact": gi,
        "messages": full_msgs[:max_messages],
        "truncated_messages": max(0, len(full_msgs) - max_messages),
        "edges": full_edges[:max_edges],
        "truncated_edges": max(0, len(full_edges) - max_edges),
    }


def aggregate_cda_reports(reports: Sequence[TrialCdaReport]) -> dict[str, Any]:
    reps = sorted(reports, key=lambda r: (r.trial_name, r.trial_id or ""))
    seats = list(dict.fromkeys(s for r in reps for s in r.roster))
    by_sender: dict[str, list[float]] = {s: [] for s in seats}
    for rep in reps:
        for msg in rep.messages:
            assert math.isfinite(msg.influence_score) and msg.influence_score >= 0.0
            by_sender.setdefault(msg.sender, []).append(msg.influence_score)
    raw_tot = {s: math.fsum(by_sender.get(s, ())) for s in seats}
    gs = [r.guard_impact for r in reps]
    locks = sum(g.claim_locks_granted for g in gs)
    saved_dups = sum(g.claim_rejections_saved_dups for g in gs)
    ub = sum(g.unread_blocks_count for g in gs)
    compliant_blocks = sum(
        round((g.post_block_fetch_compliance_rate or 0.0) * g.unread_blocks_count) for g in gs
    )
    rate = round(compliant_blocks / ub, 6) if ub > 0 else None
    chk = sum(g.hash_chain_checked_trials for g in gs)
    val = sum(g.hash_chain_valid_trials for g in gs)
    tamp = sum(g.hash_chain_tampered_trials for g in gs)
    assert val + tamp == chk
    assert rate is None or (0.0 <= rate <= 1.0)
    return {
        "messages_posted": sum(r.messages_posted for r in reps),
        "messages_fetched": sum(r.messages_fetched for r in reps),
        "avoided_duplicate_targets": sum(r.avoided_duplicate_targets for r in reps),
        "triggered_actions": sum(r.triggered_actions for r in reps),
        "ignored_messages": sum(r.ignored_messages for r in reps),
        "sender_causal_credit": normalize_sender_credit(raw_tot, seats),
        "sender_raw_credit": {s: round(raw_tot[s], 6) for s in seats},
        "compliant_unread_blocks": compliant_blocks,
        "guard_impact": {
            "claim_locks_granted": locks,
            "claim_rejections_saved_dups": saved_dups,
            "unread_blocks_count": ub,
            "post_block_fetch_compliance_rate": rate,
            "hash_chain_checked_trials": chk,
            "hash_chain_valid_trials": val,
            "hash_chain_tampered_trials": tamp,
        },
    }


def _add_int(mapping: Mapping[str, Any], key: str) -> int:
    val = mapping.get(key)
    return val if isinstance(val, int) and not isinstance(val, bool) and val >= 0 else 0


def _collect_sender_scores(
    cda: Mapping[str, Any],
    seats: list[str],
    seen_seats: set[str],
    by_sender: dict[str, list[float]],
) -> None:
    cred_map = cda.get("sender_causal_credit")
    if isinstance(cred_map, Mapping):
        for seat in cred_map:
            if isinstance(seat, str) and seat not in seen_seats:
                seen_seats.add(seat)
                seats.append(seat)
    raw_map = cda.get("sender_raw_credit")
    if isinstance(raw_map, Mapping):
        for seat, rv in raw_map.items():
            if (
                isinstance(seat, str)
                and isinstance(rv, (int, float))
                and not isinstance(rv, bool)
                and math.isfinite(float(rv))
                and float(rv) >= 0.0
            ):
                if seat not in seen_seats:
                    seen_seats.add(seat)
                    seats.append(seat)
                by_sender.setdefault(seat, []).append(float(rv))


def merge_corpus_cda(corpora: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    seats: list[str] = []
    seen_seats: set[str] = set()
    by_sender: dict[str, list[float]] = {}
    posted = fetched = avoided = triggered = ignored = 0
    locks = saved_dups = ub = compliant_blocks = chk = val = tamp = 0
    for corp in corpora:
        cda = corp.get("cda")
        if not isinstance(cda, Mapping):
            continue
        posted += _add_int(cda, "messages_posted")
        fetched += _add_int(cda, "messages_fetched")
        avoided += _add_int(cda, "avoided_duplicate_targets")
        triggered += _add_int(cda, "triggered_actions")
        ignored += _add_int(cda, "ignored_messages")
        _collect_sender_scores(cda, seats, seen_seats, by_sender)
        gi = cda.get("guard_impact")
        if isinstance(gi, Mapping):
            locks += _add_int(gi, "claim_locks_granted")
            saved_dups += _add_int(gi, "claim_rejections_saved_dups")
            c_ub = _add_int(gi, "unread_blocks_count")
            ub += c_ub
            c_comp = cda.get("compliant_unread_blocks")
            if isinstance(c_comp, int) and not isinstance(c_comp, bool):
                compliant_blocks += min(c_ub, max(0, c_comp))
            else:
                c_rate = gi.get("post_block_fetch_compliance_rate")
                r_num = (
                    float(c_rate)
                    if isinstance(c_rate, (int, float))
                    and not isinstance(c_rate, bool)
                    and math.isfinite(c_rate)
                    else 0.0
                )
                compliant_blocks += round(min(1.0, max(0.0, r_num)) * c_ub)
            chk += _add_int(gi, "hash_chain_checked_trials")
            val += _add_int(gi, "hash_chain_valid_trials")
            tamp += _add_int(gi, "hash_chain_tampered_trials")
    assert val + tamp == chk
    raw_tot = {s: math.fsum(by_sender.get(s, ())) for s in seats}
    rate = round(compliant_blocks / ub, 6) if ub > 0 else None
    assert rate is None or (0.0 <= rate <= 1.0)
    return {
        "messages_posted": posted,
        "messages_fetched": fetched,
        "avoided_duplicate_targets": avoided,
        "triggered_actions": triggered,
        "ignored_messages": ignored,
        "sender_causal_credit": normalize_sender_credit(raw_tot, seats),
        "sender_raw_credit": {s: round(raw_tot[s], 6) for s in seats},
        "compliant_unread_blocks": compliant_blocks,
        "guard_impact": {
            "claim_locks_granted": locks,
            "claim_rejections_saved_dups": saved_dups,
            "unread_blocks_count": ub,
            "post_block_fetch_compliance_rate": rate,
            "hash_chain_checked_trials": chk,
            "hash_chain_valid_trials": val,
            "hash_chain_tampered_trials": tamp,
        },
    }


HTML_CDA_SCRIPT = """\
  function fmtCompliance(rate) {
    if (typeof rate !== "number" || !Number.isFinite(rate)) return "n/a";
    return rate.toFixed(3) + " (" + (rate * 100).toFixed(1) + "%)";
  }

  function fmtCreditInline(cred) {
    const keys = Object.keys(cred || {});
    if (!keys.length) return '<span class="muted">none</span>';
    return keys.map(function (k) {
      return esc(k) + "=" + fmtNum(cred[k], 3);
    }).join(", ");
  }

  function fmtChainStatus(gi) {
    const chk = gi.hash_chain_checked_trials || 0;
    const val = gi.hash_chain_valid_trials || 0;
    const tamp = gi.hash_chain_tampered_trials || 0;
    if (tamp > 0) return esc(val + "/" + chk) + " " + badge("tampered=" + tamp, "error");
    if (chk > 0) return esc(val + "/" + chk) + " " + badge("valid", "ok");
    return '0/0 ' + badge("unchecked", "");
  }

  function renderCdaOverview() {
    const cda = ((DATA.overview || {}).cda) || {};
    const gi = cda.guard_impact || {};
    const fetched = cda.messages_fetched || 0;
    const ignored = cda.ignored_messages || 0;
    const ignSub = fetched > 0 ? fmtNum((ignored / fetched) * 100, 1) + "% of fetched" : "0 fetched";
    const cards = [
      ["Avoided Duplicate Targets", String(cda.avoided_duplicate_targets || 0), "triggered " + (cda.triggered_actions || 0) + " / posted " + (cda.messages_posted || 0)],
      ["Triggered Actions", String(cda.triggered_actions || 0), "fetched " + fetched + " / ignored " + ignored],
      ["Ignored Messages", String(ignored), ignSub],
      ["Claim-Lock Guard", String(gi.claim_locks_granted || 0) + " locks", "saved_dups " + (gi.claim_rejections_saved_dups || 0)],
      ["Unread-Message Guard", String(gi.unread_blocks_count || 0) + " blocks", "compliance " + fmtCompliance(gi.post_block_fetch_compliance_rate)],
      ["Hash-Chain Integrity", String(gi.hash_chain_valid_trials || 0) + " / " + String(gi.hash_chain_checked_trials || 0), "tampered " + (gi.hash_chain_tampered_trials || 0)],
    ];
    document.getElementById("cda-kpis").innerHTML = cards.map(function (item) {
      return '<div class="card"><div class="kpi-label">' + esc(item[0]) +
        '</div><div class="kpi-val">' + esc(item[1]) +
        '</div><div class="kpi-sub mono">' + esc(item[2]) + "</div></div>";
    }).join("");

    const corpora = DATA.corpora || [];
    const rowsHtml = corpora.length ? corpora.map(function (co) {
      const cc = co.cda || {};
      const cgi = cc.guard_impact || {};
      const ecoStr = Object.keys(co.ecosystems || {}).map(function (k) {
        return badge(k + ":" + co.ecosystems[k], "info");
      }).join(" ");
      return "<tr>" +
        "<td><strong>" + esc(co.label) + "</strong><div>" + ecoStr + "</div></td>" +
        '<td class="mono">' + esc(co.n_trials || 0) + "</td>" +
        '<td class="mono">' + esc(cc.messages_posted || 0) + " / " + esc(cc.messages_fetched || 0) + "</td>" +
        '<td class="mono">' + esc(cc.avoided_duplicate_targets || 0) + "</td>" +
        '<td class="mono">' + esc(cc.triggered_actions || 0) + "</td>" +
        '<td class="mono">' + esc(cc.ignored_messages || 0) + "</td>" +
        '<td class="mono">' + fmtCreditInline(cc.sender_causal_credit) + "</td>" +
        '<td class="mono">' + esc(cgi.claim_locks_granted || 0) + " / " + esc(cgi.claim_rejections_saved_dups || 0) + "</td>" +
        '<td class="mono">' + esc(cgi.unread_blocks_count || 0) + " (" + esc(fmtCompliance(cgi.post_block_fetch_compliance_rate)) + ")</td>" +
        '<td class="mono">' + fmtChainStatus(cgi) + "</td></tr>";
    }).join("") : '<tr><td colspan="10" class="muted">No corpora loaded.</td></tr>';
    document.getElementById("cda-corpora-panel").innerHTML =
      '<h3>Per-Corpus Causal Attribution &amp; Server Guard Matrix</h3><div class="table-wrap"><table>' +
      "<thead><tr><th>Corpus</th><th>Trials</th><th>Post / Fetch</th><th>Avoided Dups</th><th>Triggered Actions</th><th>Ignored Msgs</th><th>Sender Causal Credit</th><th>Claim Locks / Saved Dups</th><th>Unread Blocks (Compliance)</th><th>Hash Chain (Valid/Checked)</th></tr></thead>" +
      "<tbody>" + rowsHtml + "</tbody></table></div>";

    const cred = cda.sender_causal_credit || {};
    const credKeys = Object.keys(cred);
    document.getElementById("cda-credit-chart").innerHTML = svgHBarChart(
      credKeys.map(function (k) {
        const sh = typeof cred[k] === "number" && Number.isFinite(cred[k]) ? cred[k] : 0;
        return {
          label: k,
          value: sh * 100,
          text: fmtNum(sh, 3) + " (" + fmtNum(sh * 100, 1) + "%)",
          color: "var(--accent)",
        };
      })
    );
    document.getElementById("cda-guard-chart").innerHTML = svgHBarChart([
      { label: "avoided_duplicate_targets", value: cda.avoided_duplicate_targets || 0, color: "var(--ok)" },
      { label: "triggered_actions", value: cda.triggered_actions || 0, color: "var(--accent)" },
      { label: "ignored_messages", value: cda.ignored_messages || 0, color: "var(--warn)" },
      { label: "claim_locks_granted", value: gi.claim_locks_granted || 0, color: "var(--info)" },
      { label: "claim_rejections_saved_dups", value: gi.claim_rejections_saved_dups || 0, color: "var(--ok)" },
      { label: "unread_blocks_count", value: gi.unread_blocks_count || 0, color: "var(--purple)" },
      { label: "hash_chain_valid_trials", value: gi.hash_chain_valid_trials || 0, color: "var(--ok)" },
      { label: "hash_chain_tampered_trials", value: gi.hash_chain_tampered_trials || 0, color: "var(--err)" },
    ]);
  }

  function renderTrialCda(trial) {
    const tc = (trial && trial.cda) || {};
    const tgi = tc.guard_impact || {};
    const tamp = tgi.hash_chain_tampered_trials || 0;
    const chk = tgi.hash_chain_checked_trials || 0;
    const val = tgi.hash_chain_valid_trials || 0;
    const chainBadge = tamp > 0
      ? badge("hash_chain=tampered (" + val + "/" + chk + ")", "error")
      : (chk > 0 ? badge("hash_chain=valid (" + val + "/" + chk + ")", "ok") : badge("hash_chain=unchecked", ""));
    const chips = [
      badge("avoided_duplicate_targets=" + (tc.avoided_duplicate_targets || 0), (tc.avoided_duplicate_targets || 0) > 0 ? "ok" : "info"),
      badge("triggered_actions=" + (tc.triggered_actions || 0), (tc.triggered_actions || 0) > 0 ? "ok" : "info"),
      badge("ignored_messages=" + (tc.ignored_messages || 0), (tc.ignored_messages || 0) > 0 ? "warn" : "ok"),
      badge("claim_locks_granted=" + (tgi.claim_locks_granted || 0), "info"),
      badge("claim_rejections_saved_dups=" + (tgi.claim_rejections_saved_dups || 0), (tgi.claim_rejections_saved_dups || 0) > 0 ? "ok" : "info"),
      badge("unread_blocks_count=" + (tgi.unread_blocks_count || 0), "purple"),
      badge("compliance=" + fmtCompliance(tgi.post_block_fetch_compliance_rate), "info"),
      chainBadge,
    ].join(" ");
    const msgs = tc.messages || [];
    const truncM = tc.truncated_messages || 0;
    const truncE = tc.truncated_edges || 0;
    const truncNote = (truncM > 0 || truncE > 0)
      ? '<div class="muted mono" style="margin-top:6px">Showing ' + esc(msgs.length) + " message(s)" +
        (truncM > 0 ? " (+" + esc(truncM) + " truncated)" : "") +
        (truncE > 0 ? ", +" + esc(truncE) + " causal edge(s) truncated" : "") + ".</div>"
      : "";
    document.getElementById("trial-cda-summary").innerHTML =
      "<div>" + chips + "</div>" +
      '<div class="mono" style="margin-top:8px">sender_causal_credit: ' + fmtCreditInline(tc.sender_causal_credit) +
      " | posted=" + esc(tc.messages_posted || 0) + " | fetched=" + esc(tc.messages_fetched || 0) +
      " | causal_edges=" + esc((tc.edges || []).length + truncE) + "</div>" + truncNote;

    const tbody = document.getElementById("trial-cda-messages");
    if (!msgs.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="muted">No peer messages posted in this trial.</td></tr>';
      return;
    }
    tbody.innerHTML = msgs.map(function (m) {
      const avList = (m.avoided_targets || []).slice(0, 6).map(esc).join(", ");
      const trList = (m.triggered_targets || []).slice(0, 6).map(esc).join(", ");
      const dpList = (m.duplicated_targets || []).slice(0, 4).map(esc).join(", ");
      const statusBadge = m.ignored
        ? badge("ignored", "error")
        : ((m.influence_score || 0) > 0 ? badge("influential", "ok") : badge("neutral", ""));
      return "<tr>" +
        '<td class="mono">#' + esc(m.msg_id) + " (seq " + esc(m.post_seq) + ")</td>" +
        '<td class="mono"><strong>' + esc(m.sender) + "</strong></td>" +
        '<td class="mono">' + esc((m.recipients_fetched || []).join(",") || "-") + " / " + esc((m.in_context_recipients || []).join(",") || "-") + "</td>" +
        '<td class="mono">' + fmtNum(m.influence_score, 2) + "</td>" +
        '<td class="mono">' + esc(m.avoided_duplicate_targets || 0) + (avList ? ' <span class="muted">[' + avList + "]</span>" : "") + "</td>" +
        '<td class="mono">' + esc(m.triggered_actions || 0) + (trList ? ' <span class="muted">[' + trList + "]</span>" : "") + "</td>" +
        '<td class="mono">' + esc(m.contradicted_actions || 0) + (dpList ? ' <span class="muted">[' + dpList + "]</span>" : "") + "</td>" +
        "<td>" + statusBadge + "</td></tr>";
    }).join("");
  }
"""
