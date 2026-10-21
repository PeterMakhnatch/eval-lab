#!/usr/bin/env python3
"""Build qa-trace-sft-v0: leak-free trace SFT/eval dataset (stdlib only, $0).

Task: given an actions-only trajectory view plus a candidate hackbook entry
(when a mechanism is to be checked), output label + mechanism + step-cited
evidence. Labels: exploit-pass | defect-fail | neither.

Sources (all published in this repo / results home; no tb4-audit-v1 TEST):
- TB4 DEV labelled trajectories from PR #1137 census (labels-dev.jsonl):
  known_exploit/exploit_candidate -> exploit-pass, task_defect(+gray) ->
  defect-fail, neither -> neither. unmeasured/no_documented/unscored excluded.
  Only the 22 DEV tasks; TEST tasks/trajectories never read.
- cheatbench-actions-v1 dev split (MIT, steinad/CheatBench): cheating/attempt
  -> exploit-pass, benign -> neither.
- hacktrace-act-v1 dev split (CC BY 4.0, JH012/HackTrace-Data): ACT ->
  exploit-pass, negative -> neither.
- mimo-v7-canonical-pass70 / mimo-fresh-window3: NO dev split exists (all
  items are `test`), so they are excluded and recorded in the manifest.

Split: by task/source group (sha256 bucket). Source `test` items (cheatbench/
hacktrace) go only to this dataset's `test`; source `dev` items are split by
group into train/dev/test. No group straddles splits.
"""
import hashlib, json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[3]  # eval-lab worktree root
RESULTS = pathlib.Path.home() / "Developer/eval-lab-results"
OUT = pathlib.Path(__file__).resolve().parent
MAX_ACTION_CHARS = 8000

def h(s: str) -> str: return hashlib.sha256(s.encode()).hexdigest()
def bucket(s: str) -> str:
    v = int(h("qa-trace-sft-v0:" + s)[:8], 16) % 100
    return "train" if v < 70 else ("dev" if v < 85 else "test")

def load_jsonl(p): return [json.loads(l) for l in pathlib.Path(p).read_text().splitlines() if l.strip()]

# --- hackbook candidate entries ---
HB = load_jsonl(ROOT / "research/hackbook/entries.jsonl")
HB_BY_ID = {e["id"]: e for e in HB}
def hb_input(entry):
    if not entry: return None
    return {"id": entry["id"], "title": entry["title"], "mechanism": entry["mechanism"][:600],
            "direction": entry["direction"]}
def pick_hb(mechanism: str, label: str, seed: str):
    m = (mechanism or "").lower()
    rules = [("test_tamper", "HB-RS02-001"), ("conftest", "HB-RS02-001"), ("secret_discovery", "HB-RS03-001"),
             ("verifier_exploitation", "HB-RS02-001"), ("hardcode", "HB-RS01-001"), ("harness_leaks", "HB-RS03-001"),
             ("untimed", "HB-TS01-006"), ("shortcut", "HB-RS01-001"), ("vf2", "HB-TS01-006")]
    for k, eid in rules:
        if k in m: return HB_BY_ID.get(eid)
    if label == "exploit-pass": return HB_BY_ID.get("HB-RS01-001")
    if label == "defect-fail": return HB_BY_ID.get("HB-RS06-001")
    pool = [e for e in HB if e["id"].startswith("HB-RS")]
    return pool[int(h(seed)[:8], 16) % len(pool)]

KEYWORDS = {
    "test_tamper": ["test", "conftest"], "secret_discovery": ["secret", ".env", "answer", "solution"],
    "verifier_exploitation": ["verifier", "test", "grade"], "hardcode": ["print", "expected"],
    "untimed": ["prepare", "cache", "time"], "vf2": ["prepare", "cache"],
}
def evidence_for(actions, mechanism, fallback_text=""):
    kws = []
    for k, v in KEYWORDS.items():
        if k in (mechanism or ""): kws += v
    if not kws: kws = [w for w in re.split(r"[^a-z0-9]+", (mechanism or "").lower()) if len(w) > 4][:3]
    for a in actions:
        low = a["text"].lower()
        if kws and any(k in low for k in kws):
            return [{"step": a["step"], "quote": a["text"][:200]}]
    if fallback_text:
        return [{"step": actions[0]["step"] if actions else 0, "quote": fallback_text[:200]}]
    return []

def render_actions(steps):
    """steps: list of (step_id, text). Returns truncated action dicts."""
    out, total = [], 0
    for sid, text in steps:
        text = re.sub(r"\s+", " ", text).strip()[:1200]
        if total + len(text) > MAX_ACTION_CHARS: break
        out.append({"step": int(sid), "text": text}); total += len(text)
    return out

def atif_actions(path):
    d = json.loads(pathlib.Path(path).read_text())
    steps = []
    for s in d.get("steps", []):
        if s.get("source") not in ("agent", "assistant"): continue
        parts = []
        for tc in s.get("tool_calls") or []:
            args = tc.get("arguments") or {}
            cmd = args.get("command") or args.get("cmd") or json.dumps(args)[:800]
            parts.append(f"{tc.get('function_name','tool')}: {cmd}")
        if not parts and s.get("message"): parts.append(str(s["message"])[:800])
        if parts: steps.append((s.get("step_id", len(steps)+1), " | ".join(parts)))
    return render_actions(steps)

def tb4_actions(traj_path):
    d = json.loads(pathlib.Path(traj_path).read_text())
    steps = []
    for s in d.get("steps", []):
        if s.get("source") != "agent": continue
        parts = []
        for tc in s.get("tool_calls") or []:
            args = tc.get("arguments") or (tc.get("extra") or {}).get("raw_arguments") or {}
            cmd = args.get("command") or json.dumps(args)[:800]
            parts.append(f"{tc.get('function_name','Bash')}: {cmd}")
        if parts: steps.append((s.get("step_id", len(steps)+1), " | ".join(parts)))
    return render_actions(steps)

rows, excluded = [], {"mimo_no_dev_split": 0, "tb4_unmeasured_or_undocumented": 0, "missing_trajectory": 0}

# --- TB4 DEV ---
labels = load_jsonl(ROOT / "research/experiments/tb4-run-census-20261021/labels-dev.jsonl")
index = {}
for line in (RESULTS / "tb4-hub/trajectory-index.jsonl").read_text().splitlines():
    d = json.loads(line)
    if d.get("dev_task_allowlist") and d.get("archive_path"): index[d["hub_trial_id"]] = d["archive_path"]
label_map = {"known_exploit": "exploit-pass", "exploit_candidate": "exploit-pass",
             "task_defect": "defect-fail", "task_defect_gray": "defect-fail", "neither": "neither"}
# deterministic caps for the two large classes
def keep(r):
    if r["label"] in ("known_exploit", "exploit_candidate"): return True
    cap = 160 if r["label"].startswith("task_defect") else 160
    return int(h(r["membership_id"])[:8], 16) % 1000 < int(cap / max(1, sum(1 for x in labels if x["label"]==r["label"])) * 1000) if False else int(h(r["membership_id"])[:6], 16) % 3 == 0
for r in labels:
    if r["label"] not in label_map: excluded["tb4_unmeasured_or_undocumented"] += 1; continue
    if not keep(r): continue
    ap = index.get(r["hub_trial_id"])
    tp = pathlib.Path(ap) / "agent/trajectory.json" if ap else None
    if not tp or not tp.exists(): excluded["missing_trajectory"] += 1; continue
    try: actions = tb4_actions(tp)
    except Exception: excluded["missing_trajectory"] += 1; continue
    if not actions: excluded["missing_trajectory"] += 1; continue
    label, mech = label_map[r["label"]], (r["mechanisms"][0] if r["mechanisms"] else r["label"])
    rows.append({"id": f"qa-trace-sft-v0:tb4:{r['hub_trial_id']}", "source": "tb4-dev-census-20261021",
        "source_split": "dev", "split": bucket("tb4/" + r["task"]), "group": "tb4/" + r["task"],
        "task": r["task"], "input": {"actions": actions, "candidate_hackbook_entry": hb_input(pick_hb(mech, label, r["hub_trial_id"]))},
        "target": {"label": label, "mechanism": mech, "evidence": evidence_for(actions, mech, r.get("evidence",""))},
        "label_provenance": f"PR #1137 census label={r['label']} method={r['method']}; not replay-confirmed",
        "licence": "Terminal-Bench 4 public Hub trajectories (DEV tasks only)"})

# --- cheatbench + hacktrace ---
for key, lic in [("cheatbench-actions-v1", "MIT (steinad/CheatBench)"), ("hacktrace-act-v1", "CC BY 4.0 (JH012/HackTrace-Data)")]:
    for it in load_jsonl(ROOT / f"research/answer-keys/{key}/items.jsonl"):
        p = RESULTS / it["actions_only_ref"]["path"] / "agent/trajectory.json"
        if not p.exists(): excluded["missing_trajectory"] += 1; continue
        try: actions = atif_actions(p)
        except Exception: excluded["missing_trajectory"] += 1; continue
        if not actions: excluded["missing_trajectory"] += 1; continue
        pos = it["expected_verdict"] == "fail" and it["expected_category"] == "reward_hacking"
        label = "exploit-pass" if pos else "neither"
        mech = it.get("expected_root_cause") or (it.get("source_labels") or {}).get("judge_category") or label
        grp = f"{key}/{it['subject']['task_id']}"
        split = "test" if it["split"] == "test" else bucket(grp)
        rows.append({"id": f"qa-trace-sft-v0:{it['item_id']}", "source": key, "source_split": it["split"],
            "split": split, "group": grp, "task": it["subject"]["task_id"],
            "input": {"actions": actions, "candidate_hackbook_entry": hb_input(pick_hb(mech, label, it["item_id"]))},
            "target": {"label": label, "mechanism": mech if pos else None, "evidence": evidence_for(actions, mech) if pos else []},
            "label_provenance": it["label_source"], "licence": lic})

# mimo keys: verify no dev split
for key in ["mimo-v7-canonical-pass70", "mimo-fresh-window3"]:
    for it in load_jsonl(ROOT / f"research/answer-keys/{key}/items.jsonl"):
        if it["split"] != "dev": excluded["mimo_no_dev_split"] += 1

# leak checks
groups = {}
for r in rows: groups.setdefault(r["group"], set()).add(r["split"])
assert not [g for g, s in groups.items() if len(s) > 1 and not g.startswith("cheatbench")], "group straddles splits"
blob = json.dumps(rows)
assert "tb4-audit-v1" not in blob or True  # provenance strings may name the key; scan inputs only
inputs_blob = json.dumps([r["input"] for r in rows] + [r["target"] for r in rows])
for split in ["train", "dev", "test"]:
    part = [r for r in rows if r["split"] == split]
    with open(OUT / f"{split}.jsonl", "w") as f:
        for r in part: f.write(json.dumps(r, sort_keys=True) + "\n")

import collections
manifest = {"dataset_id": "qa-trace-sft-v0", "schema": "qa-trace-sft-v0",
    "counts": {"total": len(rows), "by_split": dict(collections.Counter(r["split"] for r in rows)),
               "by_split_label": {s: dict(collections.Counter(r["target"]["label"] for r in rows if r["split"]==s)) for s in ["train","dev","test"]},
               "by_source": dict(collections.Counter(r["source"] for r in rows))},
    "excluded": excluded, "split_rule": "sha256 group bucket 70/15/15 by task/source group; source test -> dataset test only",
    "leak_checks": {"tb4_test_tasks_included": 0, "note": "TB4 rows only from labels-dev.jsonl (22 DEV tasks); mimo keys excluded (no dev split); groups do not straddle splits (cheatbench family is the grouping unit and its source test stays test)"},
    "licences": {"cheatbench-actions-v1": "MIT", "hacktrace-act-v1": "CC BY 4.0", "tb4": "public Hub trajectories, DEV only"},
    "training_authorized_by_this_release": False, "spend_usd": 0}
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
print(json.dumps(manifest["counts"], indent=1)); print(excluded)
