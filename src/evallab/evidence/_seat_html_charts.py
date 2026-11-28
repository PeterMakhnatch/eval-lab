from __future__ import annotations

HTML_CHART_SCRIPT = """\
  function svgHBarChart(items, colorVar) {
    if (!items.length) return '<div class="muted">No data recorded.</div>';
    let maxVal = 1;
    for (let i = 0; i < items.length; i += 1) {
      const v = items[i] && typeof items[i].value === "number" && Number.isFinite(items[i].value) ? items[i].value : 0;
      if (v > maxVal) maxVal = v;
    }
    const rowH = 26;
    const h = items.length * rowH + 8;
    const w = 520;
    const labelW = 195;
    const barMaxW = 240;
    const rows = items.map(function (it, i) {
      const y = 6 + i * rowH;
      const val = typeof it.value === "number" && Number.isFinite(it.value) ? Math.max(0, it.value) : 0;
      const bw = Math.max(2, (val / maxVal) * barMaxW);
      const fill = esc(it.color || colorVar || "var(--accent)");
      return "<g>" +
        '<text x="' + (labelW - 8) + '" y="' + (y + 15) + '" text-anchor="end" fill="var(--text)" font-size="11" font-family="ui-monospace,monospace">' + esc(it.label) + "</text>" +
        '<rect x="' + labelW + '" y="' + (y + 4) + '" width="' + bw.toFixed(1) + '" height="16" rx="3" fill="' + fill + '" opacity="0.88"></rect>' +
        '<text x="' + (labelW + bw + 6) + '" y="' + (y + 15) + '" fill="var(--muted)" font-size="11" font-family="ui-monospace,monospace">' + esc(it.text || String(val)) + "</text>" +
        "</g>";
    }).join("");
    return '<svg class="svg-chart" viewBox="0 0 ' + w + " " + h + '" role="img">' + rows + "</svg>";
  }

  function classifyBespokeArmRole(arm, targetTeamSize) {
    const setting = String(arm.setting || "").toLowerCase();
    const label = String(arm.arm || "").toLowerCase();
    if (setting === "control" || label.indexOf("control") >= 0) return "control";
    if (arm.team_size === 1 || label === "solo") return "solo";
    if (label === "team" || (setting === "team" && targetTeamSize > 1 && arm.team_size === targetTeamSize)) return "team";
    return "other";
  }

  function svgMarkCurves(markHours, decomps) {
    if (!markHours.length || !decomps.length) return "";
    const w = 640, padL = 48, padR = 160, padT = 18, padB = 30;
    const h = Math.max(220, padT + decomps.length * 16 + 64);
    const plotW = w - padL - padR, plotH = h - padT - padB;
    let maxX = 0;
    markHours.forEach(function (hr) {
      if (typeof hr === "number" && Number.isFinite(hr) && hr > maxX) maxX = hr;
    });
    if (maxX <= 0) maxX = 1;
    const colors = { solo: "#8b9bb0", control: "#d29922", team: "#3fb950", other: "#4ea1ff" };
    const modelDash = ["", "5,3", "2,2"];
    let minY = 0;
    let maxY = 0.05;
    decomps.forEach(function (md) {
      (md.arms || []).forEach(function (a) {
        (a.mean_marks || []).forEach(function (v) {
          if (typeof v === "number" && Number.isFinite(v)) {
            if (v > maxY) maxY = v;
            if (v < minY) minY = v;
          }
        });
      });
    });
    const rawSpan = maxY - minY;
    if (Number.isFinite(rawSpan)) {
      const spanY = Math.max(0.05, rawSpan);
      const hiScaled = (maxY + spanY * 0.08) * 20;
      if (Number.isFinite(hiScaled)) maxY = Math.ceil(hiScaled) / 20;
      if (minY < 0) {
        const loScaled = (minY - spanY * 0.08) * 20;
        if (Number.isFinite(loScaled)) minY = Math.floor(loScaled) / 20;
      }
    }
    const rangeY = maxY - minY;
    const useHalf = !Number.isFinite(rangeY);
    const denom = useHalf ? Math.max(1e-9, maxY / 2 - minY / 2) : Math.max(1e-9, rangeY);
    const sx = function (x) { return padL + (x / maxX) * plotW; };
    const sy = function (y) {
      const ratio = useHalf ? (y / 2 - minY / 2) / denom : (y - minY) / denom;
      return padT + plotH - Math.max(0, Math.min(1, Number.isFinite(ratio) ? ratio : 0)) * plotH;
    };
    let grid = "";
    for (let g = 0; g <= 4; g += 1) {
      const frac = g / 4;
      const yv = minY * (1 - frac) + maxY * frac;
      const yy = sy(yv);
      const yLbl = Math.abs(yv) >= 1e6 ? yv.toExponential(1) : yv.toFixed(2);
      grid += '<line x1="' + padL + '" y1="' + yy.toFixed(1) + '" x2="' + (w - padR) + '" y2="' + yy.toFixed(1) + '" stroke="var(--border)" stroke-dasharray="2,2"></line>';
      grid += '<text x="' + (padL - 6) + '" y="' + (yy + 4).toFixed(1) + '" text-anchor="end" fill="var(--muted)" font-size="10">' + yLbl + "</text>";
    }
    markHours.forEach(function (hr) {
      if (typeof hr !== "number" || !Number.isFinite(hr)) return;
      const xx = sx(hr);
      grid += '<text x="' + xx.toFixed(1) + '" y="' + (h - 8) + '" text-anchor="middle" fill="var(--muted)" font-size="10">' + esc(hr) + "h</text>";
    });
    let paths = "";
    let legend = "";
    let ly = padT + 10;
    const distinctTargetKs = new Set();
    decomps.forEach(function (md, mi) {
      const dash = modelDash[mi % modelDash.length];
      let ctrlK = 0;
      let mdMaxK = 1;
      (md.arms || []).forEach(function (a) {
        const st = String(a.setting || "").toLowerCase();
        const lb = String(a.arm || "").toLowerCase();
        if (typeof a.team_size === "number" && a.team_size > mdMaxK) mdMaxK = a.team_size;
        if ((st === "control" || lb.indexOf("control") >= 0) && typeof a.team_size === "number" && a.team_size > ctrlK) ctrlK = a.team_size;
      });
      const targetK = ctrlK > 0 ? ctrlK : (mdMaxK > 1 ? mdMaxK : 0);
      if (targetK > 1) distinctTargetKs.add(targetK);
      let bestLegend = null;
      const roleRank = { team: 3, control: 2, solo: 1, other: 0 };
      (md.arms || []).forEach(function (a) {
        const pts = [];
        const coords = [];
        markHours.forEach(function (hr, hi) {
          const val = (a.mean_marks || [])[hi];
          if (typeof hr === "number" && Number.isFinite(hr) && typeof val === "number" && Number.isFinite(val)) {
            const cx = sx(hr);
            const cy = sy(val);
            pts.push(cx.toFixed(1) + "," + cy.toFixed(1));
            coords.push([cx, cy]);
          }
        });
        if (!pts.length) return;
        const role = classifyBespokeArmRole(a, targetK);
        const col = colors[role] || "#4ea1ff";
        if (pts.length >= 2) {
          paths += '<polyline fill="none" stroke="' + col + '" stroke-width="2" ' +
            (dash ? 'stroke-dasharray="' + dash + '" ' : "") +
            'points="' + pts.join(" ") + '"></polyline>';
        }
        coords.forEach(function (pt) {
          paths += '<circle cx="' + pt[0].toFixed(1) + '" cy="' + pt[1].toFixed(1) + '" r="2.6" fill="' + col + '"></circle>';
        });
        const rk = roleRank[role] != null ? roleRank[role] : 0;
        if (!bestLegend || rk > bestLegend.rank) {
          const kTag = role === "solo" ? "S1" : (role === "control" ? "S" + (a.team_size || "N") + "^0" : (a.team_size ? "S" + a.team_size : "Team"));
          bestLegend = { rank: rk, col: col, kTag: kTag };
        }
      });
      if (bestLegend) {
        legend += '<line x1="' + (w - padR + 10) + '" y1="' + ly + '" x2="' + (w - padR + 28) + '" y2="' + ly + '" stroke="' + bestLegend.col + '" stroke-width="2" ' + (dash ? 'stroke-dasharray="' + dash + '"' : "") + "></line>" +
          '<text x="' + (w - padR + 34) + '" y="' + (ly + 4) + '" fill="var(--text)" font-size="10">' + esc(md.model_slug) + " (" + esc(bestLegend.kTag) + ")</text>";
        ly += 16;
      }
    });
    const teamTag = distinctTargetKs.size === 1 ? "S" + Array.from(distinctTargetKs)[0] : "SN";
    legend += '<text x="' + (w - padR + 10) + '" y="' + (ly + 8) + '" fill="var(--ok)" font-size="10">Green: Team (' + teamTag + ")</text>";
    legend += '<text x="' + (w - padR + 10) + '" y="' + (ly + 22) + '" fill="var(--warn)" font-size="10">Amber: Control (' + teamTag + "^0)</text>";
    legend += '<text x="' + (w - padR + 10) + '" y="' + (ly + 36) + '" fill="var(--muted)" font-size="10">Gray: Solo (S1)</text>';
    return '<svg class="svg-chart" viewBox="0 0 ' + w + " " + h + '" role="img">' + grid + paths + legend + "</svg>";
  }
"""
