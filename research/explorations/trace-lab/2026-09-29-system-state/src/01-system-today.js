// Poster 1 — "Trace Lab today: how one MiMo task runs". Facts: local://poster-spec.md (verified 2026-09-29).
// Render: %load kit.js, %load this file, then `await render(sceneSystemToday(), "<dir>/01-system-today")`.
globalThis.sceneSystemToday = function sceneSystemToday() {
  resetIds();
  const els = [];
  const add = (...xs) => els.push(...xs.flat(Infinity));
  const card = ({ x, y, w, h, t, b, col, size = 13.5, font = FONT.hand }) => [
    rect({ x, y, w, h, stroke: col[0], fill: "#ffffff", sw: 2 }),
    text({ x: x + 12, y: y + 8, t, size: 16, font: FONT.head, color: C.ink }),
    para({ x: x + 12, y: y + 34, w: w - 24, h: h - 42, t: b, size, font }),
  ];
  const dot = (cx, cy, n, col = C.navy) => [
    ellipse({ x: cx - 12, y: cy - 12, w: 24, h: 24, stroke: col, fill: "#ffffff", sw: 2 }),
    text({ x: cx, y: cy + 1, t: String(n), size: 14, font: FONT.head, color: col, align: "center", valign: "middle" }),
  ];
  const legendItem = (x, label, col) => [
    ellipse({ x, y: 118, w: 22, h: 22, stroke: col[0], fill: col[1], sw: 2 }),
    text({ x: x + 28, y: 119, t: label, size: 14, color: C.ink }),
  ];

  // ---- header
  add(
    text({ x: 30, y: 18, t: "Trace Lab today: how one MiMo task runs", size: 46, font: FONT.head, color: C.navy }),
    text({ x: 33, y: 84, t: "As built on 2026-09-29. Checked against code, run folders, Linear receipts and live Modal/billing reads.", size: 17, color: C.muted }),
    note({ x: 1750, y: 12, w: 590, h: 128, col: C.yellow, size: 17, angle: -0.012,
      t: "Overnight: 44 runs, all scored\n8 passes (7 clean)\nModal billed $13.68 today\n(nothing running 17:05Z)" }),
    text({ x: 30, y: 118, t: "status:", size: 14, font: FONT.head, color: C.muted }),
    legendItem(110, "GREEN works+verified", C.green),
    legendItem(360, "YELLOW manual/partial", C.yellow),
    legendItem(620, "RED broken/missing", C.red),
    legendItem(870, "GRAY unknown", C.gray),
  );

  // ---- zones
  add(zone({ x: 30, y: 165, w: 340, h: 1060, title: "TASKS", col: C.blue }));
  add(zone({ x: 400, y: 165, w: 560, h: 1060, title: "EXPERIMENT + APPROVAL", col: C.purple }));
  add(zone({ x: 990, y: 165, w: 770, h: 1060, title: "EXECUTION · biggest zone", col: C.teal }));
  add(zone({ x: 1790, y: 165, w: 550, h: 1060, title: "RECORDS + ANALYSIS", col: C.green }));

  // ---- Zone 1 cards
  add(
    card({ x: 50, y: 235, w: 300, h: 210, col: C.blue, t: "FineEnvs task snapshots",
      b: "6 Hugging Face repos pinned\nby revision: code, cyber,\ngeneral, music, terminal, webdev\n7,780 tasks in the task store\nwe use code, cyber, terminal only" }),
    card({ x: 50, y: 457, w: 300, h: 190, col: C.blue, t: "Sealed split",
      b: "salt har81-sealed-20260928\nheld-out tasks are never\ntrained on, never tuned against\ntrain pool of 48 (HAR-85)" }),
    card({ x: 50, y: 659, w: 300, h: 250, col: C.blue, t: "Nop qualification",
      b: "run each task with an agent\nthat does nothing, on Daytona\na grader that errors or passes\nhere is broken\nHAR-88: 113 tasks → 3 broken\nHAR-95: 13/13 ok ($0.04)" }),
    card({ x: 50, y: 921, w: 300, h: 250, col: C.red, t: "Suspect graders (HAR-97)",
      b: "1634 checks an exact source line\n1789 needs a missing library\n(stevedore)\n1702 missing tqdm\n→ proposed exclusions" }),
  );

  // ---- Zone 2 cards
  add(
    card({ x: 420, y: 235, w: 520, h: 220, col: C.purple, t: "Experiment README",
      b: "question, pinned setup, task list,\ncost table (expected + worst),\nstop rules, receipt format\nstrong for HAR-81/90/85;\nweaker for HAR-83/86" }),
    card({ x: 420, y: 467, w: 520, h: 330, col: C.purple, t: "Treatment key = 'same setup'", size: 12.5, font: FONT.code,
      b: "commit 48b787b5 · normalizer eb61671b\nMiMo-V2.6-Distill-Qwen-9B @ 2367e865\nSGLang image 00b02004 · 64K context\nT 0.6 · top-p 0.95 · top-k 20\n4,096 out · thinking on\nTerminus-2 · Harbor 0.21.0\nsummarize at 16,384\nlimits: 200 calls · 2.5M in · 900 s term\n3,600 s code+cyber\n→ hashed sha256:04b4e45c" }),
    card({ x: 420, y: 809, w: 520, h: 330, col: C.purple, t: "Queue",
      b: "submit → approve --actor peter\n→ tick\npolicy gate: $3 per job,\n$20 per UTC day,\nstop after 3 failures in a row\n(breaker)" }),
  );

  // ---- Zone 3: hub-and-spoke around the controller. Left column = controller (hub)
  // + reply normalizer; right column = proxy, Modal, Daytona; verifier full-width below.
  // Modal and Daytona never talk to each other.
  add(
    card({ x: 1010, y: 235, w: 260, h: 240, col: C.teal, t: "Terminus-2 controller",
      b: "Harbor 0.21 · runs on the Mac\nin an Eval Lab worktree\nsends the prompt, reads the reply\ntypes keystrokes into the sandbox,\nreads the screen back\nsummarizes when the context fills" }),
    card({ x: 1010, y: 560, w: 260, h: 190, col: C.teal, t: "MiMo reply normalizer",
      b: "turns MiMo's own tool-call formats\ninto Terminus commands\n(6 shapes + task_complete\n+ prose completion)\n96.3% of pilot replies parsed\nunescaped quotes fixed in #536" }),
    card({ x: 1360, y: 235, w: 380, h: 140, col: C.teal, t: "Capture proxy + ledger",
      b: "every model call is logged with tokens\neach call reconciled or marked\nunresolved (never silently dropped)" }),
    card({ x: 1360, y: 605, w: 380, h: 140, col: C.teal, t: "Modal · SGLang server",
      b: "app evallab-mimo-v26-9b · 1x A100-80GB · $2.81/h\nmax 1 container · scales to zero after 5 idle min\nstopped by hand after each wave" }),
    card({ x: 1360, y: 775, w: 380, h: 150, col: C.teal, t: "Daytona sandbox (one per trial)",
      b: "Tier 2 · lifetime cap 70 min (TTL)\nstops after 5 idle min · deleted on stop\nabout $0.08/h terminal\nabout $0.23/h code+cyber" }),
    card({ x: 1010, y: 1140, w: 730, h: 80, col: C.teal, t: "Verifier",
      b: "the task's own tests run in the sandbox → reward 1.0 / 0.0 · scored even at limits" }),
  );
  // hub spokes: controller -> proxy (prompt out); proxy <-> Modal (model calls);
  // proxy -> normalizer -> controller (reply -> commands); controller <-> Daytona
  // (keystrokes in, screen out); Daytona -> verifier (tests -> reward)
  add(
    // controller -> proxy: prompt out
    arr(1270, 310, 1360, 310, { stroke: C.navy, sw: 2.5 }),
    text({ x: 1315, y: 272, t: "prompt out", size: 13, font: FONT.code, color: C.navy, align: "center" }),
    // proxy <-> Modal: model calls (vertical, both ends arrowed)
    arr(1550, 375, 1550, 605, { stroke: C.navy, sw: 2.5, head: "arrow", tail: "arrow" }),
    text({ x: 1570, y: 482, t: "model calls", size: 13, font: FONT.code, color: C.navy }),
    // reply path: proxy -> rail -> normalizer, then normalizer -> controller
    path([[1360, 360], [1300, 360], [1300, 530], [1240, 530], [1240, 560]], { stroke: C.navy, sw: 2.5 }),
    text({ x: 1300, y: 388, t: "reply back", size: 13, font: FONT.code, color: C.navy }),
    arr(1040, 560, 1040, 475, { stroke: C.navy, sw: 2.5 }),
    text({ x: 1060, y: 530, t: "reply → commands", size: 13, font: FONT.code, color: C.navy }),
    // controller <-> Daytona: keystrokes in, screen out (far-left rail)
    path([[1010, 460], [1000, 460], [1000, 795], [1360, 795]], { stroke: C.navy, sw: 2.5, head: "arrow", tail: "arrow" }),
    text({ x: 1030, y: 768, t: "keystrokes in, screen out", size: 13, font: FONT.code, color: C.navy }),
    // Daytona -> verifier: tests -> reward
    arr(1470, 925, 1470, 1140, { stroke: C.navy, sw: 2.5 }),
    text({ x: 1490, y: 1020, t: "tests → reward", size: 13, font: FONT.code, color: C.navy }),
    dot(1315, 240, 3), dot(1512, 490, 4), dot(1240, 770, 5), dot(1505, 1060, 6),
  );

  // ---- Zone 4 cards
  add(
    card({ x: 1810, y: 235, w: 510, h: 330, col: C.green, t: "One trial folder", size: 12.5, font: FONT.code,
      b: "result.json — reward, tokens,\ntimes, exception\ntrajectory.json (+ .cont-N)\n— every step: proposed /\naccepted / executed / observed\nrecording.cast — terminal video\nverifier/ — reward.txt, test out,\nagent.diff\nlab-metadata.json — command,\ndigests, usage ledger\njob.log, lock.json, config.json" }),
    card({ x: 1810, y: 605, w: 510, h: 160, col: C.green, t: "Catalog (parquet)",
      b: "trial_treatment (setup key),\ntrial_capture (tokens, gaps),\nmodel_calls\npool check refuses mixed setups" }),
    card({ x: 1810, y: 805, w: 510, h: 170, col: C.green, t: "Tagger · probe-03 (Traces)",
      b: "first failure, harness vs model,\nloops, stuck terminals, taint\n43/44 agree with blind hand reads\nrun by hand" }),
    card({ x: 1810, y: 1015, w: 510, h: 160, col: C.green, t: "SFT export",
      b: "passing runs only, held-out\nrefused, secret scan, taint out\n→ 7 conversations selected\n(curated v3)" }),
  );
  // dot 8 arrow: trial folder -> catalog gutter
  add(arr(2065, 565, 2065, 605, { stroke: C.navy, sw: 2 }), dot(2105, 585, 8));

  // ---- inter-zone arrows with dots 1, 2, 7
  add(
    arr(370, 550, 400, 550, { stroke: C.navy, sw: 2.5 }), dot(385, 512, 1),
    arr(960, 310, 1002, 310, { stroke: C.navy, sw: 2.5 }), dot(975, 272, 2),
    arr(1740, 1180, 1790, 1180, { stroke: C.navy, sw: 2.5 }), dot(1775, 1142, 7),
  );

  // ---- CONTROL strip
  add(rect({ x: 30, y: 1245, w: 2310, h: 96, stroke: C.gray[0], fill: C.gray[2], sw: 2 }),
    text({ x: 48, y: 1257, t: "CONTROL", size: 20, font: FONT.head, color: C.gray[0] }),
    para({ x: 48, y: 1286, w: 2304, h: 50, size: 14,
      t: "Linear cards (lin) = work queue + receipts · Herdr tabs: Research, Engineering, Data, Infra, Traces, Reef · MISSION.md = decisions · Peter approves anything paid." }),
  );

  // ---- bottom band: money + teardown table
  add(text({ x: 30, y: 1362, t: "Is the infrastructure correct? (money + teardown)", size: 23, font: FONT.head, color: C.navy }));
  const tx = 30, tw = 2310;
  const cCheck = 430, cStatus = 170;
  const rows = [
    ["Modal stops costing when idle", "GREEN", C.green, "scales to 0 after 5 min; app list empty, 0 containers at 17:05Z"],
    ["Modal stopped after each wave", "YELLOW", C.yellow, "done by hand; one stop 2.5 min late (about $0.12)"],
    ["Daytona sandboxes cannot leak", "GREEN", C.green, "70-min lifetime cap, auto-stop 5 min, deleted on stop"],
    ["Daytona state checkable now", "GRAY", C.gray, "CLI unauthorized here; last check 11:27Z showed 0 sandboxes"],
    ["$3/job and $20/day caps in code", "GREEN", C.green, "queue.py PolicyGate"],
    ["Caps see real MiMo spend", "RED", C.red, "trial cost null for every MiMo run; $20 gate sees estimates only"],
    ["Every model call accounted", "GREEN", C.green, "ledger marks unresolved calls (0758-c: 93 flagged)"],
    ["Token totals honest", "RED", C.red, "unresolved calls add reserved-but-never-sent tokens (0758-c 26.1M vs 5.5M)"],
    ["Killed job still counted", "RED", C.red, "a4-000240 killed while saving: no metadata, missing from catalog + spend"],
    ["Bills reconciled", "YELLOW", C.yellow, "by hand in Linear receipts; Modal $13.68 matches; Daytona has no per-sandbox bill"],
  ];
  const rh = 50, ty = 1400;
  add(
    rect({ x: tx, y: ty, w: cCheck, h: 40, stroke: C.ink, fill: C.gray[1], sw: 1, round: false }),
    rect({ x: tx + cCheck, y: ty, w: cStatus, h: 40, stroke: C.ink, fill: C.gray[1], sw: 1, round: false }),
    rect({ x: tx + cCheck + cStatus, y: ty, w: tw - cCheck - cStatus, h: 40, stroke: C.ink, fill: C.gray[1], sw: 1, round: false }),
    para({ x: tx + 8, y: ty + 4, w: cCheck - 16, h: 32, t: "Check", size: 15, font: FONT.head, valign: "middle" }),
    para({ x: tx + cCheck + 8, y: ty + 4, w: cStatus - 16, h: 32, t: "Status", size: 15, font: FONT.head, valign: "middle" }),
    para({ x: tx + cCheck + cStatus + 8, y: ty + 4, w: tw - cCheck - cStatus - 16, h: 32, t: "Evidence", size: 15, font: FONT.head, valign: "middle" }),
  );
  rows.forEach(([check, status, col, ev], i) => {
    const y = ty + 40 + i * rh;
    const bg = i % 2 ? "#ffffff" : "#f1f3f5";
    add(
      rect({ x: tx, y, w: cCheck, h: rh, stroke: "#c9ccd1", fill: bg, sw: 1, round: false }),
      rect({ x: tx + cCheck, y, w: cStatus, h: rh, stroke: "#c9ccd1", fill: bg, sw: 1, round: false }),
      rect({ x: tx + cCheck + cStatus, y, w: tw - cCheck - cStatus, h: rh, stroke: "#c9ccd1", fill: bg, sw: 1, round: false }),
      para({ x: tx + 10, y: y + 4, w: cCheck - 20, h: rh - 8, t: check, size: 14, valign: "middle" }),
      rect({ x: tx + cCheck + 12, y: y + 9, w: cStatus - 24, h: 32, stroke: col[0], fill: col[1], sw: 1.5,
        label: { text: status, size: 13, font: FONT.head, color: C.ink } }),
      para({ x: tx + cCheck + cStatus + 10, y: y + 4, w: tw - cCheck - cStatus - 20, h: rh - 8, t: ev, size: 13.5, valign: "middle" }),
    );
  });

  add(text({ x: 2340, y: 1948, align: "right",
    t: "Sources: eval-lab har81-dispatch-531 (queue.py, database.py, harbor_daytona.py, serve.py, treatment-key.json); Linear HAR-81/88/90/93/95/97/100; modal list + billing 2026-09-29 17:05Z. Drawn by Research-Harbor, 2026-09-29.",
    size: 12, font: FONT.code, color: C.muted }));
  return els;
};
void 0;
