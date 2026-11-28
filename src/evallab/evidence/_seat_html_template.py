from __future__ import annotations

from ._seat_html_cda import HTML_CDA_SCRIPT
from ._seat_html_charts import HTML_CHART_SCRIPT
from ._seat_html_styles import HTML_STYLES, build_html_shell

__all__ = ["HTML_SCRIPT", "HTML_STYLES", "build_html_document"]

_HTML_SCRIPT_HEAD = """\
(function () {
  "use strict";
  const rawEl = document.getElementById("forensics-data");
  const DATA = JSON.parse(rawEl ? rawEl.textContent : "{}");
  let filteredTrials = DATA.trials || [];
  let activeTrialIdx = 0;
  let activeKindFilter = "ALL";
  let activeActorFilter = "ALL";

  function esc(val) {
    return String(val == null ? "" : val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtNum(v, digits) {
    if (typeof v !== "number" || !Number.isFinite(v)) return "n/a";
    return v.toFixed(digits == null ? 2 : digits);
  }

  function fmtDelta(v, digits) {
    if (typeof v !== "number" || !Number.isFinite(v)) return '<span class="muted">n/a</span>';
    const cls = v > 1e-9 ? "pos" : (v < -1e-9 ? "neg" : "muted");
    const sign = v > 0 ? "+" : "";
    return '<span class="' + cls + '">' + sign + v.toFixed(digits == null ? 4 : digits) + "</span>";
  }

  function fmtCost(v) {
    if (typeof v !== "number" || !Number.isFinite(v)) return "unknown";
    return "$" + v.toFixed(4);
  }

  function fmtCi(ci) {
    if (!Array.isArray(ci) || ci.length !== 2) return "n/a";
    return "[" + fmtNum(ci[0], 3) + ", " + fmtNum(ci[1], 3) + "]";
  }

  function badge(text, cls) {
    return '<span class="badge ' + esc(cls || "") + '">' + esc(text) + "</span>";
  }

  function initTheme() {
    const btn = document.getElementById("theme-toggle");
    const root = document.documentElement;
    btn.addEventListener("click", function () {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      btn.textContent = next === "light" ? "Dark Theme" : "Light Theme";
    });
  }

  function renderHeaderMeta() {
    document.getElementById("report-title").textContent = DATA.title || "Eval Lab Multi-Agent Forensics";
    const corpora = DATA.corpora || [];
    const meta = [
      "Schema: " + (DATA.schema_version || ""),
      "Corpora: " + (corpora.length || 1),
      "Total Trials/Runs: " + (DATA.n_trials || 0),
      "Embedded Interactive Trials: " + (DATA.trials || []).length,
      "Source: " + (DATA.target_path || ""),
    ].join(" | ");
    document.getElementById("report-subtitle").textContent = meta;
  }
"""

_HTML_SCRIPT_BODY = """\
  function renderOverview() {
    const o = DATA.overview || {};
    const f = o.funnel || {};
    const c = o.collisions || {};
    const i = o.integrity_summary || {};
    const modeCounts = o.failure_mode_counts || {};
    const totalEps = Object.values(modeCounts).reduce(function (a, b) { return a + b; }, 0);
    const cards = [
      ["Total Trials / Runs", String(DATA.n_trials || 0), Object.keys(o.ecosystems || {}).map(function (k) { return k + ":" + o.ecosystems[k]; }).join(", ") || "none"],
      ["Message Funnel", String(f.messages_posted || 0) + " posted", "fetch " + (f.messages_fetched || 0) + " / ctx " + (f.messages_in_context || 0) + " / act " + (f.messages_acted_on || 0)],
      ["Unread at Action", String(f.unread_at_action || 0), "never_fetched " + (f.never_fetched || 0)],
      ["Collisions & Races", String((c.duplicate_target_actions || 0) + (c.stale_read_write_races || 0)), "dup " + (c.duplicate_target_actions || 0) + " / stale_rw " + (c.stale_read_write_races || 0) + " / post_eop " + (c.post_end_of_play_events || 0)],
      ["Failure Episodes", String(totalEps), Object.keys(modeCounts).length + " active mode(s)"],
      ["Completeness", String(i.logs_complete || 0) + " logs / " + String(i.cost_complete || 0) + " cost", "err_w_reward " + (i.seat_error_with_reward || 0) + " / auth_mm " + (i.unauthenticated_author_mismatch || 0)],
    ];
    document.getElementById("overview-kpis").innerHTML = cards.map(function (item) {
      return '<div class="card"><div class="kpi-label">' + esc(item[0]) +
        '</div><div class="kpi-val">' + esc(item[1]) +
        '</div><div class="kpi-sub mono">' + esc(item[2]) + "</div></div>";
    }).join("");

    const corpora = DATA.corpora || [];
    const corpEl = document.getElementById("corpora-panel");
    if (corpora.length) {
      corpEl.innerHTML = '<h3>Cross-Ecosystem Corpus Matrix</h3><div class="table-wrap"><table>' +
        '<thead><tr><th>Corpus</th><th>Ecosystems</th><th>Trials</th><th>Events / Links</th><th>Post / Fetch / Ctx / Act</th><th>Unread@Act</th><th>Episodes</th><th>Logs / Cost Complete</th><th>Known USD</th></tr></thead><tbody>' +
        corpora.map(function (co) {
          const cf = co.funnel || {};
          const ci = co.integrity_summary || {};
          const eps = Object.values(co.failure_mode_counts || {}).reduce(function (a, b) { return a + b; }, 0);
          const ecoStr = Object.keys(co.ecosystems || {}).map(function (k) { return badge(k + ":" + co.ecosystems[k], "info"); }).join(" ");
          return "<tr>" +
            "<td><strong>" + esc(co.label) + '</strong><div class="muted mono">' + esc(co.source_path) + "</div></td>" +
            "<td>" + ecoStr + "</td>" +
            '<td class="mono">' + esc(co.n_trials) + "</td>" +
            '<td class="mono">' + esc(co.events_total || 0) + " / " + esc(co.causal_links_total || 0) + "</td>" +
            '<td class="mono">' + esc(cf.messages_posted || 0) + " / " + esc(cf.messages_fetched || 0) + " / " + esc(cf.messages_in_context || 0) + " / " + esc(cf.messages_acted_on || 0) + "</td>" +
            '<td class="mono">' + esc(cf.unread_at_action || 0) + "</td>" +
            '<td class="mono">' + badge(String(eps), eps > 0 ? "warn" : "ok") + "</td>" +
            '<td class="mono">' + esc(ci.logs_complete || 0) + " / " + esc(ci.cost_complete || 0) + "</td>" +
            '<td class="mono">' + (typeof co.known_cost_usd === "number" && Number.isFinite(co.known_cost_usd) ? "$" + fmtNum(co.known_cost_usd, 2) : "unknown") + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    } else {
      corpEl.classList.add("hidden");
    }

    document.getElementById("funnel-chart").innerHTML = svgHBarChart([
      { label: "delivery_pairs", value: f.message_delivery_pairs || 0, color: "var(--muted)" },
      { label: "messages_posted", value: f.messages_posted || 0, color: "var(--info)" },
      { label: "messages_fetched", value: f.messages_fetched || 0, color: "var(--purple)" },
      { label: "messages_in_context", value: f.messages_in_context || 0, color: "var(--accent)" },
      { label: "messages_acted_on", value: f.messages_acted_on || 0, color: "var(--ok)" },
      { label: "never_fetched", value: f.never_fetched || 0, color: "var(--warn)" },
    ]);

    const modeKeys = Object.keys(modeCounts).sort(function (a, b) { return modeCounts[b] - modeCounts[a]; });
    document.getElementById("modes-chart").innerHTML = svgHBarChart(
      modeKeys.map(function (k) { return { label: k, value: modeCounts[k], color: "var(--warn)" }; })
    );
    const modeEl = document.getElementById("overview-modes");
    modeEl.innerHTML = modeKeys.length
      ? modeKeys.map(function (k) {
          return '<button type="button" class="pill mode-jump" data-mode="' + esc(k) + '">' + esc(k) + ": " + esc(modeCounts[k]) + "</button>";
        }).join(" ")
      : '<span class="muted">No failure episodes detected across loaded trials.</span>';
    modeEl.querySelectorAll(".mode-jump").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const sel = document.getElementById("filter-mode");
        const val = btn.getAttribute("data-mode") || "ALL";
        if (sel) {
          sel.value = val;
          applyTrialFilters();
        }
      });
    });
  }

  function renderDecompositions() {
    const decomps = DATA.decompositions || [];
    const sec = document.getElementById("decomp-section");
    if (!decomps.length) {
      sec.classList.add("hidden");
      return;
    }
    sec.classList.remove("hidden");
    document.getElementById("decomp-panels").innerHTML = decomps.map(function (d) {
      const armKeys = ["solo", "equal_budget_solo", "silent", "team"].filter(function (k) { return d.arms && d.arms[k]; });
      const armRows = armKeys.map(function (k) {
        const a = d.arms[k];
        const p = a.pace || {};
        const costRatio = p.cost_ratio_vs_solo_paired != null ? p.cost_ratio_vs_solo_paired : p.cost_ratio_vs_solo;
        return "<tr>" +
          "<td><strong>" + esc(k) + "</strong></td>" +
          '<td class="mono">' + esc(a.seats_min) + (a.seats_max !== a.seats_min ? "-" + esc(a.seats_max) : "") + "</td>" +
          '<td class="mono">' + esc(a.n_tasks_shared) + " / " + esc(a.n_trials_shared) + "</td>" +
          '<td class="mono"><strong>' + fmtNum(a.score_macro, 4) + "</strong></td>" +
          '<td class="mono">' + esc(fmtCi(a.ci_rerun)) + " / " + esc(fmtCi(a.ci_tasks)) + "</td>" +
          '<td class="mono">' + esc(fmtCost(p.mean_cost_usd)) + (costRatio != null ? " (" + fmtNum(costRatio, 2) + "x)" : "") + "</td>" +
          '<td class="mono">' + (p.mean_total_tokens != null ? fmtNum(p.mean_total_tokens / 1e6, 2) + "M" : "n/a") + "</td>" +
          '<td class="mono">' + (p.mean_wall_time_sec != null ? fmtNum(p.mean_wall_time_sec, 1) + "s" : "n/a") + " / " + (p.mean_agent_hours != null ? fmtNum(p.mean_agent_hours, 3) + "h" : "n/a") + "</td>" +
          '<td class="mono">' + fmtNum(p.score_per_usd, 3) + "</td></tr>";
      }).join("");
      const effKeys = ["headcount", "coordination", "total", "budget_scaling", "fragmentation_gap", "silent_fragmentation_gap"].filter(function (k) { return d.effects && d.effects[k]; });
      const effRows = effKeys.map(function (k) {
        const e = d.effects[k];
        const mcStr = e.p_mcnemar != null ? " / mc=" + fmtNum(e.p_mcnemar, 4) : "";
        return "<tr>" +
          "<td><strong>" + esc(k) + "</strong> (" + esc(e.candidate_role) + " - " + esc(e.baseline_role) + ")</td>" +
          '<td class="mono">' + fmtDelta(e.difference, 4) + "</td>" +
          '<td class="mono">' + esc(fmtCi(e.ci_rerun)) + " (p=" + fmtNum(e.p_rerun, 4) + ")</td>" +
          '<td class="mono">' + esc(fmtCi(e.ci_tasks)) + "</td>" +
          '<td class="mono">t=' + fmtNum(e.p_paired_t, 4) + " / sf=" + fmtNum(e.p_signflip, 4) + (e.signflip_exact ? " exact" : "") + mcStr + "</td>" +
          '<td class="mono">' + esc(e.candidate_better) + "W / " + esc(e.baseline_better) + "L / " + esc(e.ties) + "T</td>" +
          "<td>" + badge(e.verdict_rerun, e.verdict_rerun === "resolved" ? "ok" : "info") + " " + badge(e.verdict_tasks, e.verdict_tasks === "resolved" ? "ok" : "info") + "</td></tr>";
      }).join("");
      const bok = d.best_of_k_solo || null;
      const bokScore = bok ? (bok.unbiased_score_macro != null ? bok.unbiased_score_macro : bok.empirical_max_macro) : null;
      const shareBadges = [
        badge("shared_tasks=" + (d.shared_tasks || []).length, "info"),
        d.headcount_share != null ? badge("headcount_share=" + fmtNum(d.headcount_share * 100, 1) + "%", "purple") : "",
        d.coordination_share != null ? badge("coordination_share=" + fmtNum(d.coordination_share * 100, 1) + "%", "ok") : "",
        d.recovery_ratio != null ? badge("recovery=" + fmtNum(d.recovery_ratio * 100, 1) + "%", "ok") : "",
        d.fragmentation_recovery_ratio != null ? badge("frag_recovery=" + fmtNum(d.fragmentation_recovery_ratio * 100, 1) + "%", "purple") : "",
        bokScore != null ? badge("best_of_" + bok.k + "_solo=" + fmtNum(bokScore, 4), "info") : "",
      ].filter(Boolean).join(" ");
      return '<div class="card" style="margin-bottom:14px">' +
        '<h3>' + esc(d.label || "3-Arm Decomposition") + " " + shareBadges + "</h3>" +
        '<div class="table-wrap"><table><thead><tr><th>Arm</th><th>Seats</th><th>Tasks / Trials</th><th>Macro Score</th><th>95% CI (Rerun / Task)</th><th>Mean Cost (vs Solo)</th><th>Mean Tokens</th><th>Wall / Agent-h</th><th>Score / $</th></tr></thead><tbody>' +
        armRows + "</tbody></table></div>" +
        '<div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Effect</th><th>Delta</th><th>Rerun 95% CI (p)</th><th>Task 95% CI</th><th>Paired-t / Sign-flip p</th><th>Task W/L/T</th><th>Verdicts</th></tr></thead><tbody>' +
        effRows + "</tbody></table></div></div>";
    }).join("");
  }

  function renderBespokePublic() {
    const items = DATA.bespoke_public || [];
    const sec = document.getElementById("bespoke-section");
    if (!items.length) {
      sec.classList.add("hidden");
      return;
    }
    sec.classList.remove("hidden");
    document.getElementById("bespoke-panels").innerHTML = items.map(function (bp) {
      const decomps = bp.decompositions || [];
      const distinctTasks = new Set(decomps.map(function (md) { return md.task_slug || ""; }).filter(Boolean));
      const showTask = distinctTasks.size > 1;
      const targetKs = new Set();
      decomps.forEach(function (md) {
        let ctrlK = 0;
        let maxK = 1;
        (md.arms || []).forEach(function (a) {
          const st = String(a.setting || "").toLowerCase();
          const lb = String(a.arm || "").toLowerCase();
          if (typeof a.team_size === "number" && a.team_size > maxK) maxK = a.team_size;
          if ((st === "control" || lb.indexOf("control") >= 0) && typeof a.team_size === "number" && a.team_size > ctrlK) ctrlK = a.team_size;
        });
        const tk = ctrlK > 0 ? ctrlK : (maxK > 1 ? maxK : 0);
        if (tk > 1) targetKs.add(tk);
      });
      const teamKLabel = targetKs.size === 1 ? String(Array.from(targetKs)[0]) : "N";
      const rows = decomps.map(function (md) {
        const subMeta = showTask && md.task_slug ? md.task_slug + " | " + (md.harness || md.model_slug) : (md.harness || md.model_slug);
        return "<tr>" +
          "<td><strong>" + esc(md.model_name) + '</strong><div class="muted mono">' + esc(subMeta) + "</div></td>" +
          '<td class="mono">' + fmtNum(md.solo_mean, 4) + "</td>" +
          '<td class="mono">' + fmtNum(md.control_mean, 4) + "</td>" +
          '<td class="mono"><strong>' + fmtNum(md.team_mean, 4) + "</strong></td>" +
          '<td class="mono">' + fmtDelta(md.headcount_gain, 4) + "</td>" +
          '<td class="mono">' + fmtDelta(md.coordination_gain, 4) + "</td>" +
          '<td class="mono">' + fmtDelta(md.total_gain, 4) + "</td>" +
          '<td class="mono">' + (md.cost_ratio_team_vs_solo != null ? fmtNum(md.cost_ratio_team_vs_solo, 2) + "x" : "n/a") + "</td>" +
          '<td class="mono">' + (md.token_ratio_team_vs_solo != null ? fmtNum(md.token_ratio_team_vs_solo, 2) + "x" : "n/a") + "</td></tr>";
      }).join("");
      const markHrs = bp.mark_hours || [];
      const markTitle = markHrs.length ? "Checkpoint Trajectory (" + esc(markHrs.join("h, ")) + "h)" : "Checkpoint Trajectory";
      return '<div class="card" style="margin-bottom:14px">' +
        '<h3>' + esc(bp.label) + " " + badge(bp.n_runs + " runs", "info") + " " +
        badge(bp.n_with_board_log + " board log(s)", "purple") + " " +
        badge(bp.n_with_agent_trajectories + " trajectory rollout(s)", "ok") + "</h3>" +
        '<div class="grid-2" style="margin-bottom:0">' +
        '<div class="table-wrap"><table><thead><tr><th>Model / Harness</th><th>S_1</th><th>S_' + esc(teamKLabel) + '^0</th><th>S_' + esc(teamKLabel) + '</th><th>Headcount</th><th>Coord</th><th>Total</th><th>Cost Ratio</th><th>Tok Ratio</th></tr></thead><tbody>' +
        rows + "</tbody></table></div>" +
        "<div><h3>" + markTitle + "</h3>" + svgMarkCurves(markHrs, decomps) + "</div>" +
        "</div></div>";
    }).join("");
  }

  function populateSelect(el, opts, allLabel) {
    el.innerHTML = '<option value="ALL">' + esc(allLabel) + "</option>" +
      opts.map(function (v) { return '<option value="' + esc(v) + '">' + esc(v) + "</option>"; }).join("");
  }

  function initTrialFilters() {
    const trials = DATA.trials || [];
    const corpora = Array.from(new Set(trials.map(function (t) { return t.corpus_label || "default"; }))).sort();
    const ecos = Array.from(new Set(trials.map(function (t) { return t.ecosystem || "unknown"; }))).sort();
    const tasks = Array.from(new Set(trials.map(function (t) { return t.task_name || "unknown"; }))).sort();
    const modeSet = new Set(Object.keys(((DATA.overview || {}).failure_mode_counts) || {}));
    trials.forEach(function (t) {
      Object.keys(t.failure_mode_counts || {}).forEach(function (k) { modeSet.add(k); });
    });
    const modes = Array.from(modeSet).sort();

    populateSelect(document.getElementById("filter-corpus"), corpora, "All Corpora");
    populateSelect(document.getElementById("filter-eco"), ecos, "All Ecosystems");
    populateSelect(document.getElementById("filter-task"), tasks, "All Tasks");
    const modeEl = document.getElementById("filter-mode");
    modeEl.innerHTML = '<option value="ALL">All Modes</option><option value="NONE">Zero Episodes</option>' +
      modes.map(function (m) { return '<option value="' + esc(m) + '">' + esc(m) + "</option>"; }).join("");
    const integEl = document.getElementById("filter-integrity");
    integEl.innerHTML = [
      ["ALL", "All Integrity"],
      ["CLEAN", "Clean (logs & cost ok)"],
      ["LOGS_INCOMPLETE", "Logs Incomplete"],
      ["COST_INCOMPLETE", "Cost Incomplete"],
      ["ERR_REWARD", "Seat Error w/ Reward"],
      ["AUTH_MISMATCH", "Auth Mismatch"],
      ["CHAIN_TAMPERED", "Hash-Chain Tampered"],
    ].map(function (p) { return '<option value="' + p[0] + '">' + p[1] + "</option>"; }).join("");

    ["filter-corpus", "filter-eco", "filter-task", "filter-mode", "filter-integrity"].forEach(function (id) {
      document.getElementById(id).addEventListener("change", applyTrialFilters);
    });
    document.getElementById("trial-select").addEventListener("change", function () {
      activeTrialIdx = Number(document.getElementById("trial-select").value) || 0;
      activeKindFilter = "ALL";
      activeActorFilter = "ALL";
      renderActiveTrial();
    });
    applyTrialFilters();
  }

  function applyTrialFilters() {
    const fc = document.getElementById("filter-corpus").value;
    const fe = document.getElementById("filter-eco").value;
    const ft = document.getElementById("filter-task").value;
    const fm = document.getElementById("filter-mode").value;
    const fi = document.getElementById("filter-integrity").value;
    const allTrials = DATA.trials || [];
    filteredTrials = allTrials.filter(function (t) {
      if (fc !== "ALL" && (t.corpus_label || "default") !== fc) return false;
      if (fe !== "ALL" && (t.ecosystem || "unknown") !== fe) return false;
      if (ft !== "ALL" && (t.task_name || "unknown") !== ft) return false;
      const mc = t.failure_mode_counts || {};
      const totalM = Object.values(mc).reduce(function (a, b) { return a + b; }, 0);
      if (fm === "NONE" && totalM > 0) return false;
      if (fm !== "ALL" && fm !== "NONE" && !(Object.prototype.hasOwnProperty.call(mc, fm) && mc[fm] > 0)) return false;
      const ig = t.integrity || {};
      if (fi === "CLEAN" && !(ig.logs_complete && ig.cost_complete && !ig.seat_error_with_reward && !ig.unauthenticated_author_mismatch)) return false;
      if (fi === "LOGS_INCOMPLETE" && ig.logs_complete) return false;
      if (fi === "COST_INCOMPLETE" && ig.cost_complete) return false;
      if (fi === "ERR_REWARD" && !ig.seat_error_with_reward) return false;
      if (fi === "AUTH_MISMATCH" && !ig.unauthenticated_author_mismatch) return false;
      if (fi === "CHAIN_TAMPERED" && !((((t.cda || {}).guard_impact) || {}).hash_chain_tampered_trials > 0)) return false;
      return true;
    });
    const sel = document.getElementById("trial-select");
    sel.innerHTML = "";
    filteredTrials.forEach(function (t, idx) {
      const opt = document.createElement("option");
      opt.value = String(idx);
      const modes = Object.values(t.failure_mode_counts || {}).reduce(function (a, b) { return a + b; }, 0);
      opt.textContent = (idx + 1) + ". [" + (t.corpus_label || t.ecosystem) + "] " + t.trial_name + " (" + modes + " ep)";
      sel.appendChild(opt);
    });
    document.getElementById("trial-match-count").textContent =
      "Showing " + filteredTrials.length + " / " + allTrials.length + " embedded trials";
    activeTrialIdx = 0;
    activeKindFilter = "ALL";
    activeActorFilter = "ALL";
    renderActiveTrial();
  }

  function eventCssClass(ev) {
    if (ev.kind === "message_post") return "post";
    if (ev.kind === "message_fetch") return "fetch";
    if (ev.kind === "auth_violation" || ev.phase === "post_play") return "err";
    if (ev.kind === "policy_refusal" || ev.kind === "probe") return "warn";
    return "action";
  }

  function renderSwimlane(trial) {
    const tl = trial.timeline || {};
    const events = tl.events || [];
    const container = document.getElementById("swimlane-container");
    const detail = document.getElementById("event-detail");
    detail.textContent = "Click any event node on the swimlane to inspect full provenance.";

    const members = Object.keys(trial.members || {});
    if (!events.length) {
      container.innerHTML = '<div class="muted">No world, board, or git events recorded for this trial.</div>';
    } else {
      let minX = Infinity;
      let maxX = -Infinity;
      for (let i = 0; i < events.length; i += 1) {
        const xv = events[i].x_pos;
        if (typeof xv === "number" && Number.isFinite(xv)) {
          if (xv < minX) minX = xv;
          if (xv > maxX) maxX = xv;
        }
      }
      if (!Number.isFinite(minX)) {
        minX = 0;
        maxX = 1;
      }
      const span = maxX > minX ? (maxX - minX) : 1;
      const eopX = tl.end_of_play_x;

      const byActor = Object.create(null);
      members.forEach(function (m) { byActor[m] = []; });
      events.forEach(function (ev, idx) {
        const a = ev.actor || "system";
        if (!byActor[a]) byActor[a] = [];
        byActor[a].push({ ev: ev, idx: idx });
      });

      const membersMap = trial.members || {};
      const truncEvHtml = (tl.truncated_events || 0) > 0
        ? '<div class="muted mono" style="margin-bottom:6px">Showing ' + esc(events.length) + " events (+" + esc(tl.truncated_events) + " truncated).</div>"
        : "";
      container.innerHTML = truncEvHtml + Object.keys(byActor).map(function (actor) {
        const mrec = Object.prototype.hasOwnProperty.call(membersMap, actor) ? membersMap[actor] : null;
        const label = mrec && mrec.display_name ? actor + " (" + mrec.display_name + ")" : actor;
        const nodes = byActor[actor].map(function (pair) {
          const ev = pair.ev;
          const pct = 3 + ((ev.x_pos - minX) / span) * 94;
          const tag = ev.short_label || ev.kind;
          return '<button type="button" class="ev-node ' + eventCssClass(ev) +
            '" style="left:' + pct.toFixed(2) + '%" data-ev-idx="' + pair.idx + '">' +
            esc(tag) + "</button>";
        }).join("");
        let eopHtml = "";
        if (eopX != null && eopX >= minX && eopX <= maxX) {
          const epct = 3 + ((eopX - minX) / span) * 94;
          eopHtml = '<div class="eop-line" style="left:' + epct.toFixed(2) + '%" title="End of play"></div>';
        }
        return '<div class="lane-row"><div class="lane-label mono">' + esc(label) +
          '</div><div class="lane-track">' + eopHtml + nodes + "</div></div>";
      }).join("");

      container.querySelectorAll(".ev-node").forEach(function (btn) {
        btn.addEventListener("click", function () {
          const ev = events[Number(btn.getAttribute("data-ev-idx"))];
          if (!ev) return;
          detail.innerHTML = '<div class="mono">' +
            "<strong>" + esc(ev.kind) + "</strong> (" + esc(ev.raw_kind || ev.source_format) + ") | " +
            "actor=" + esc(ev.actor || "none") + " | seq=" + esc(ev.seq) +
            " | step_id=" + esc(ev.step_id == null ? "unlinked" : ev.step_id) +
            " | ts=" + esc(ev.ts || "n/a") + " | target=" + esc(ev.target || "n/a") +
            (ev.msg_id ? " | msg_id=" + esc(ev.msg_id) : "") +
            (ev.text ? "<br/>text: " + esc(ev.text) : "") +
            "</div>";
        });
      });
    }

    const traces = tl.message_traces || [];
    const traceEl = document.getElementById("message-traces");
    if (!traces.length) {
      traceEl.innerHTML = '<div class="muted">No peer message funnel traces in this trial.</div>';
    } else {
      traceEl.innerHTML = traces.map(function (tr) {
        const postChip = badge("posted #" + tr.msg_id + " by " + tr.sender + " (seq " + tr.post_seq + ")", "info");
        const fetchChip = tr.fetch_seq != null
          ? badge("fetched by " + tr.recipient + " (seq " + tr.fetch_seq + ")", "info")
          : badge("never_fetched by " + tr.recipient, "warn");
        const ctxChip = tr.in_context_step != null
          ? badge("in_context step " + tr.in_context_step, "ok")
          : badge("not in_context", "warn");
        const actChip = tr.acted_on
          ? badge("acted_on" + (tr.act_seq != null ? " (seq " + tr.act_seq + ")" : ""), "ok")
          : badge("not acted_on", tr.in_context_step != null ? "error" : "warn");
        return '<div class="trace-row">' + postChip + '<span class="arrow">-&gt;</span>' +
          fetchChip + '<span class="arrow">-&gt;</span>' + ctxChip + '<span class="arrow">-&gt;</span>' +
          actChip + (tr.snippet ? '<span class="muted mono">' + esc(tr.snippet) + "</span>" : "") + "</div>";
      }).join("");
    }
  }

  function renderEpisodes(trial) {
    const eps = trial.failure_episodes || [];
    const kinds = Array.from(new Set(eps.map(function (e) { return e.kind; }))).sort();
    const actors = Array.from(new Set(eps.map(function (e) { return e.actor || "unknown"; }))).sort();

    const pills = ['<button type="button" class="pill' + (activeKindFilter === "ALL" ? " active" : "") +
      '" data-kind="ALL">All (' + eps.length + ")</button>"];
    if ((trial.truncated_failure_episodes || 0) > 0) {
      pills.push(badge("+" + trial.truncated_failure_episodes + " truncated", "warn"));
    }
    kinds.forEach(function (k) {
      const cnt = eps.filter(function (e) { return e.kind === k; }).length;
      pills.push('<button type="button" class="pill' + (activeKindFilter === k ? " active" : "") +
        '" data-kind="' + esc(k) + '">' + esc(k) + " (" + cnt + ")</button>");
    });
    const pillBar = document.getElementById("episode-pills");
    pillBar.innerHTML = pills.join("");
    pillBar.querySelectorAll(".pill").forEach(function (btn) {
      btn.addEventListener("click", function () {
        activeKindFilter = btn.getAttribute("data-kind") || "ALL";
        renderEpisodes(trial);
      });
    });

    const actorSel = document.getElementById("actor-filter");
    actorSel.innerHTML = '<option value="ALL">All actors</option>' +
      actors.map(function (a) {
        return '<option value="' + esc(a) + '"' + (activeActorFilter === a ? " selected" : "") + ">" + esc(a) + "</option>";
      }).join("");
    actorSel.onchange = function () {
      activeActorFilter = actorSel.value || "ALL";
      renderEpisodes(trial);
    };

    const filtered = eps.filter(function (e) {
      const okK = activeKindFilter === "ALL" || e.kind === activeKindFilter;
      const okA = activeActorFilter === "ALL" || (e.actor || "unknown") === activeActorFilter;
      return okK && okA;
    });
    const tbody = document.getElementById("episode-rows");
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">No matching failure episodes.</td></tr>';
      return;
    }
    tbody.innerHTML = filtered.map(function (e) {
      return "<tr>" +
        "<td>" + badge(e.kind, e.severity) + "</td>" +
        '<td class="mono">' + esc(e.actor || "unknown") + "</td>" +
        '<td class="mono">' + esc(e.step_id == null ? "-" : e.step_id) + "</td>" +
        '<td class="mono">' + esc(e.event_seq == null ? "-" : e.event_seq) + "</td>" +
        '<td class="mono">' + esc(e.ts || "-") + "</td>" +
        '<td class="mono">' + esc(e.target || "-") + "</td>" +
        "<td>" + esc(e.summary) + "</td></tr>";
    }).join("");
  }

  function renderSeatsAndIntegrity(trial) {
    const rows = Object.keys(trial.members || {}).map(function (mid) {
      const m = trial.members[mid];
      const mf = m.funnel || {};
      return "<tr>" +
        '<td class="mono"><strong>' + esc(mid) + "</strong>" +
        (m.display_name ? '<div class="muted">' + esc(m.display_name) + "</div>" : "") + "</td>" +
        '<td class="mono">' + esc(m.harness || m.agent_name || "-") + "<br/>" + esc(m.model_name || "-") + "</td>" +
        "<td>" + badge(m.trajectory_status, m.logs_complete ? "ok" : "error") + "</td>" +
        '<td class="mono">' + esc(m.n_steps == null ? "-" : m.n_steps) + " / " + esc(m.linked_step_events || 0) + "</td>" +
        '<td class="mono">' + esc(fmtCost(m.cost_usd)) + " " + badge(m.cost_status, m.cost_complete ? "ok" : "warn") + "</td>" +
        '<td class="mono">' + esc(mf.messages_posted || 0) + " / " + esc(mf.messages_fetched || 0) +
        " / " + esc(mf.messages_in_context || 0) + " / " + esc(mf.messages_acted_on || 0) + "</td>" +
        '<td class="mono">' + esc(mf.unread_at_action || 0) + " / " + esc(mf.never_fetched || 0) + "</td>" +
        "<td>" + (m.exception_type ? badge(m.exception_type, "error") : badge("ok", "ok")) + "</td></tr>";
    });
    document.getElementById("seat-rows").innerHTML = rows.join("") ||
      '<tr><td colspan="8" class="muted">No members recorded.</td></tr>';

    const ig = trial.integrity || {};
    const flags = [
      ["logs_complete", ig.logs_complete ? "ok" : "error"],
      ["cost_complete", ig.cost_complete ? "ok" : "warn"],
      ["event_log_present", ig.event_log_present ? "ok" : "warn"],
      ["seat_error_with_reward", ig.seat_error_with_reward ? "error" : "ok"],
      ["unauthenticated_author_mismatch", ig.unauthenticated_author_mismatch ? "error" : "ok"],
    ];
    const rewardsStr = trial.rewards ? JSON.stringify(trial.rewards) : "none";
    const marksHtml = (trial.grade_marks || []).length
      ? '<div class="mono" style="margin-top:6px">grade_marks: ' +
        trial.grade_marks.map(function (g) {
          return esc(g.mark) + "=" + esc(g.reward == null ? "n/a" : fmtNum(g.reward, 4));
        }).join(", ") + "</div>"
      : "";
    const issuesHtml = (ig.issues || []).length
      ? '<div class="mono muted" style="margin-top:6px">issues: ' + esc(ig.issues.join("; ")) + "</div>"
      : "";
    document.getElementById("integrity-panel").innerHTML =
      "<div>" + flags.map(function (fl) { return badge(fl[0] + "=" + String(ig[fl[0]]), fl[1]); }).join(" ") + "</div>" +
      '<div class="mono" style="margin-top:8px">corpus: ' + esc(trial.corpus_label || trial.ecosystem) +
      " | rewards: " + esc(rewardsStr) +
      " | total_cost_usd: " + esc(fmtCost(trial.total_cost_usd)) +
      " | known_cost_usd: " + esc(fmtCost(trial.known_cost_usd)) +
      (trial.comms_disabled ? " | comms_disabled=true (" + esc(trial.board_policy || "silent") + ")" : "") +
      "</div>" + marksHtml + issuesHtml;
  }

  function renderActiveTrial() {
    const sec = document.getElementById("trial-section");
    if (!filteredTrials.length) {
      sec.classList.add("hidden");
      return;
    }
    sec.classList.remove("hidden");
    const trial = filteredTrials[Math.min(activeTrialIdx, filteredTrials.length - 1)];
    renderSwimlane(trial);
    renderTrialCda(trial);
    renderEpisodes(trial);
    renderSeatsAndIntegrity(trial);
  }

  initTheme();
  renderHeaderMeta();
  renderOverview();
  renderCdaOverview();
  renderDecompositions();
  renderBespokePublic();
  initTrialFilters();
})();
"""

HTML_SCRIPT = _HTML_SCRIPT_HEAD + HTML_CHART_SCRIPT + HTML_CDA_SCRIPT + _HTML_SCRIPT_BODY


def build_html_document(title: str, payload_json: str) -> str:
    return build_html_shell(title, payload_json, HTML_SCRIPT)
