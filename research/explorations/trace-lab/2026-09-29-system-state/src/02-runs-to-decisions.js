// Poster 2 — "From runs to decisions: the missing system". Facts: local://poster-spec.md (status as of 17:10Z).
// Render: %load kit.js, %load this file, then `await render(sceneRunsToDecisions(), "<dir>/02-runs-to-decisions")`.
globalThis.sceneRunsToDecisions = function sceneRunsToDecisions() {
  resetIds();
  const els = [];
  const add = (...xs) => els.push(...xs.flat(Infinity));
  const dot = (cx, cy, n, col = C.navy) => [
    ellipse({ x: cx - 12, y: cy - 12, w: 24, h: 24, stroke: col, fill: "#ffffff", sw: 2 }),
    text({ x: cx, y: cy + 1, t: String(n), size: 14, font: FONT.head, color: col, align: "center", valign: "middle" }),
  ];
  const legendItem = (x, label, col) => [
    ellipse({ x, y: 118, w: 22, h: 22, stroke: col[0], fill: col[1], sw: 2 }),
    text({ x: x + 28, y: 119, t: label, size: 14, color: C.ink }),
  ];
  const chipR = (x, y, w, t, col) =>
    rect({ x, y, w, h: 30, stroke: col[0], fill: col[1], sw: 1.5,
      label: { text: t, size: 13, font: FONT.head, color: C.ink } });

  // ---- header
  add(
    text({ x: 30, y: 18, t: "From runs to decisions: the missing system", size: 46, font: FONT.head, color: C.navy }),
    text({ x: 33, y: 84, t: "We can run experiments now. What's missing is a fixed way to choose them and a fixed way to process what comes back.", size: 17, color: C.muted }),
    note({ x: 1750, y: 12, w: 590, h: 128, col: C.yellow, size: 17, angle: -0.012,
      t: "Today every analysis is a script\nan agent runs by hand. Nothing\nhappens automatically\nwhen a run lands." }),
    text({ x: 30, y: 118, t: "status:", size: 14, font: FONT.head, color: C.muted }),
    legendItem(110, "GREEN works+verified", C.green),
    legendItem(360, "YELLOW manual/partial", C.yellow),
    legendItem(620, "RED broken/missing", C.red),
    legendItem(870, "GRAY unknown", C.gray),
  );

  // ---- loop panels: top row y200 h520; bottom row y750 h660
  // P1
  add(panel({ x: 30, y: 200, w: 500, h: 520, n: 1, title: "Ask a question", col: C.purple }));
  add(para({ x: 50, y: 280, w: 460, h: 420, size: 14.5,
    t: "Every experiment names\nthe decision it informs.\n\nExample: does the confirmation fix\ncut tokens without losing passes?\n→ ship it in the next setup or not." }));

  // P2 with status rows
  add(panel({ x: 560, y: 200, w: 530, h: 520, n: 2, title: "Write the card first", col: C.purple }));
  const p2 = [
    ["question + decision rule", "in READMEs HAR-81/90/85, no template", "YELLOW", C.yellow],
    ["setup pinned; change ONE thing", "treatment key vs control", "GREEN", C.green],
    ["sealed + nop + graders out", "sealed GREEN, graders YELLOW", "GREEN", C.green],
    ["how many runs, and why", "sizes set by budget, not detection", "RED", C.red],
    ["metrics declared up front", "pass, clean pass, stops, parse, loops", "YELLOW", C.yellow],
    ["budget: expected + worst + stop", "cost table + stop rule", "GREEN", C.green],
  ];
  p2.forEach(([a, b, s, col], i) => {
    const y = 282 + i * 72;
    add(
      para({ x: 578, y, w: 360, h: 68, size: 13.5, t: "• " + a + "\n  " + b }),
      chipR(948, y + 8, 124, s, col),
    );
  });

  // P3 mini run flow
  add(panel({ x: 1120, y: 200, w: 550, h: 520, n: 3, title: "Run", col: C.teal }));
  const flow = ["queue", "approval (peter)", "Modal + Daytona", "verifier"];
  flow.forEach((s, i) => {
    const y = 292 + i * 88;
    add(rect({ x: 1180, y, w: 430, h: 52, stroke: C.teal[0], fill: "#ffffff", sw: 2,
      label: { text: s, size: 15, font: FONT.head, color: C.ink } }));
    if (i < 3) add(arr(1395, y + 52, 1395, y + 88, { stroke: C.navy, sw: 2 }));
  });
  add(chipR(1180, 648, 200, "GREEN · scored at limits", C.green));

  // loop arrows top row + right side
  add(
    arr(530, 420, 560, 420, { stroke: C.navy, sw: 2.5 }), dot(545, 390, 1, C.purple),
    arr(1090, 420, 1120, 420, { stroke: C.navy, sw: 2.5 }), dot(1105, 390, 2, C.purple),
    arr(1640, 720, 1640, 750, { stroke: C.navy, sw: 2.5 }), dot(1670, 735, 3, C.teal),
  );

  // P6 bottom-left
  add(panel({ x: 30, y: 750, w: 500, h: 660, n: 6, title: "Send to its owner", col: C.blue }));
  add(para({ x: 50, y: 830, w: 460, h: 560, size: 13.5,
    t: "• result real? → validity gate\n  2684 pass tainted: forbidden network install\n• harness bug → Engineering\n  reply formats: 99.5% parsed after 3 fixes\n• broken task → Data:\n  graders 1634 / 1789 / 1702\n• training data → SFT export\n  7 clean passing conversations\n• model limits → next question\n  cyber: 1 of 6 reproduced the crash" }));

  // P5
  add(panel({ x: 560, y: 750, w: 530, h: 660, n: 5, title: "Keep detectors honest", col: C.orange }));
  add(
    para({ x: 578, y: 830, w: 494, h: 220, size: 14,
      t: "Hand-read a small blind sample\nof every run (e.g. 5 traces,\nstratified: pass / fail / oddity),\nscore agreement, keep the numbers.\n\nDone for probe-03 and the wedge rule.\nReef's 5-trace audit found\n4 corrections + 2 omissions." }),
    chipR(578, 1060, 130, "YELLOW", C.yellow),
    text({ x: 716, y: 1063, t: "done for probe-03 + wedge; not routine", size: 14, color: C.ink }),
  );

  // P4 checklist
  add(panel({ x: 1120, y: 750, w: 550, h: 660, n: 4, title: "Auto-process on ingest", col: C.green }));
  const p4 = [
    ["1. integrity: scored? ledger? cost?", "cost + killed-job gaps", "RED", C.red],
    ["2. like-with-like: pool check", "exists, run by hand", "YELLOW", C.yellow],
    ["3. step records: 4 layers", "recorded at runtime", "GREEN", C.green],
    ["4. detectors (probe-03, by hand):", "parse, loops, stuck, ceiling, taint, drift", "YELLOW", C.yellow],
    ["5. tags: first failure + owner", "43/44 validated, by hand", "YELLOW", C.yellow],
    ["6. run report + catalog + SFT pick", "with reasons, by hand", "YELLOW", C.yellow],
  ];
  p4.forEach(([a, b, s, col], i) => {
    const y = 832 + i * 72;
    add(
      para({ x: 1138, y, w: 330, h: 68, size: 13.5, t: "• " + a + "\n  " + b }),
      chipR(1478, y + 8, 124, s, col),
    );
  });
  add(para({ x: 1138, y: 1268, w: 474, h: 120, size: 13,
    t: "note: stitching continuation files\nis written 4 separate times today\n→ one shared library" }));

  // loop arrows bottom row + left side
  add(
    arr(1120, 1080, 1090, 1080, { stroke: C.navy, sw: 2.5 }), dot(1105, 1110, 4, C.green),
    arr(560, 1080, 530, 1080, { stroke: C.navy, sw: 2.5 }), dot(545, 1110, 5, C.orange),
    arr(45, 752, 45, 718, { stroke: C.navy, sw: 2.5 }), dot(75, 735, 6, C.blue),
  );

  // ---- side column: experiment ladder
  add(zone({ x: 1700, y: 200, w: 670, h: 1210, title: "EXPERIMENT LADDER · proposed", col: C.blue }));
  const steps = [
    ["0 · $0 fixes first", "$0", "write real cost per trial; catch killed jobs;\nexclude suspect graders; rotate key;\nauto-process on ingest"],
    ["1 · Freeze the harness", "$0", "confirmation-loop fix checked by replay\nabout 22% fewer tokens, 0 edits cut\n→ new treatment key"],
    ["2 · Held-out baseline (anchor)", "≈$31 / $218 worst", "frozen setup, held-out tasks\nanchor for every later claim"],
    ["3 · Collect training data", "≈$80 est.", "train pool: 48 tasks x 8\n≈ 384 runs at ≈$0.21/run [estimate]"],
    ["4 · Fine-tune the distill", "unpriced", "train on clean passes\ntraining cost not yet priced"],
    ["5 · Same tasks, new weights", "", "same held-out tasks, same setup\ncompare task by task with step 2"],
  ];
  steps.forEach(([t, cost, b], i) => {
    const y = 282 + i * 160;
    add(
      text({ x: 1722, y, t, size: 16, font: FONT.head, color: C.ink }),
      ...(cost ? [chipR(1722, y + 26, cost.length > 6 ? 200 : 130, cost, C.blue)] : []),
      para({ x: 1722, y: y + (cost ? 62 : 30), w: 626, h: 128, size: 13.5, t: b }),
    );
  });
  add(para({ x: 1722, y: 1248, w: 626, h: 140, size: 13.5,
    t: "Change one thing at a time.\nHarness changes go before the baseline,\nor the baseline goes stale." }));

  // ---- bottom strip: sample size + gray LANDSCAPE note
  add(rect({ x: 30, y: 1435, w: 2340, h: 300, stroke: C.red[0], fill: C.red[2], sw: 2 }),
    text({ x: 48, y: 1449, t: "Why the number of runs matters", size: 22, font: FONT.head, color: C.red[0] }));
  add(para({ x: 48, y: 1488, w: 1400, h: 230, size: 14.5,
    t: "• 44 runs at an 18% pass rate → the true rate could be\n  anywhere from about 7% to about 29% (±11 points)\n• to see a +10-point gain reliably: about 277 runs per arm\n  (95% confidence, 80% power, runs on different tasks);\n  pairing the same tasks across arms needs fewer\n• 4 attempts per task only separates “never” from “sometimes”" }));
  add(rect({ x: 1480, y: 1488, w: 872, h: 230, stroke: C.gray[0], fill: "#ffffff", sw: 1.5 }),
    text({ x: 1496, y: 1500, t: "where outside tools fit", size: 15, font: FONT.head, color: C.gray[0] }),
    para({ x: 1496, y: 1528, w: 840, h: 180, size: 13,
      t: "Traces' survey (trace-lab/LANDSCAPE.md):\nInspect Scout and Docent are the field's\ncentre; both import Harbor runs. None handle\nour continuation files or MiMo's in-text\ncommands → our shared stitching library\nfeeds them." }));

  add(text({ x: 2370, y: 1757, align: "right",
    t: "Sources: AnalysisInventory + SystemMap audits 2026-09-29; Linear HAR-81/91/93/94/96/97/99/100; experiment READMEs (HAR-81/85/90); LANDSCAPE.md. Status as of 17:10Z.",
    size: 12, font: FONT.code, color: C.muted }));
  return els;
};
void 0;
