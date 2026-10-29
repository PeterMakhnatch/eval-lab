# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""Assemble /private/tmp/np35/w-f.json from out-f-<id>.json (worker F)."""

import json, sys

sys.dont_write_bytecode = True
REG = {
    "sleight-bench": (43, 43, 43),
    "apollo-insider-trading": (4255, 5345, 0),
    "hack-ignition": (17154, 12606, 5418),
    "assistant-axis": (157, 635, 0),
    "cuaverifierbench": (77, 29, 29),
    "collusion-probe-pitfalls": (956, 3966, 0),
    "paichecker": (1085, 1502, 1502),
    "automationbench-verified": (210, 0, 0),
}
APPLIC = {"sleight-bench": False, "cuaverifierbench": False}
NA_DESC = {
    "apollo-insider-trading": "_steps are chat dialogue (user/agent text messages, original_role field) ending in one newly generated completion; no tool_calls on any step. Native labels (string-match is_misaligned; GPT-4 is_deceptive, is_doubling_down) are per completion.",
    "hack-ignition": "_steps = exactly one agent step holding a single-turn OLMo-7B MBPP code completion; no tool_calls, no file edits (one-shot code text).",
    "assistant-axis": "_steps are user/agent chat messages (plus reasoning_content) ending in the selected response turn; no tool_calls on any step.",
    "collusion-probe-pitfalls": "_steps are public-channel committee/code-review chat messages and votes (user/agent text, input_role); no tool_calls. The separate _calls field is LLM prompt/response round-trips with a channel field, not tool calls.",
    "paichecker": "No _steps at all: each spec is a benchmark task (auditor_input: issue statement, test patch, expected tests, gold patch) judged for issue/PR/test misalignment; there is no agent run.",
    "automationbench-verified": "No _steps at all: each spec is an AutomationBench task with original prompt and original grader; there is no agent run, and no certified-clean negative exists.",
}
TASKKEY = {
    "sleight-bench": "No native task id. Primary key = s_shim.task_hash(pair_key) where pair_key is the adapter's hashed construction-group id (40 groups; attack+benign arms share it) -- a PROXY (a construction group of 1 or 2 segments). Alternate readings reported: segment_pair_key (43), scenario_key, category_key (11).",
    "cuaverifierbench": "Native task id: Online-Mind2Web task_id -> adapter task_key (sha256[:16]) -> s_shim.task_hash. One fixed run per task, so keys = runs (106).",
    "apollo-insider-trending": "",
    "apollo-insider-trading": "Adapter task_key (hash of experiment + optional scenario; excludes generator/phase/response) -> s_shim.task_hash. PROXY: 26 experiment/scenario cells, not tasks per se. Alternate: cluster_key (9,600 root runs).",
    "hack-ignition": "Native MBPP task key (adapter task_key, 60 tasks) -> s_shim.task_hash. 60 tasks, shared by all conditions/steps/seeds.",
    "assistant-axis": "Adapter problem_key (evaluator-only hashed problem id) -> s_shim.task_hash. PROXY-ish: 41 problems; 686 episodes (episode_key) is the alternate reading.",
    "collusion-probe-pitfalls": "identity.task_id (scenario id, 86) -> s_shim.task_hash. PROXY: scenario, not task per se (each scenario has many runs/conditions). Alternate: identity.run_id (7,214 distinct over 8,211 rows).",
    "paichecker": "Native instance_id prefixed with sub_corpus -> s_shim.task_hash (one row per task, 2,587). Repo (52 = 41+11) is the cluster/proxy reading.",
    "automationbench-verified": "Native example_id -> s_shim.task_hash (one row per task, 800). task_name and domain (7) are alternate/cluster readings.",
}
out = {"corpora": {}}
for cid, (p, n, e) in REG.items():
    d = json.load(open(f"/private/tmp/np35/scripts/out-f-{cid}.json"))
    g = d["groups"]
    groups = {k: v for k, v in g.items()}
    est = g.get("negative_established", {}).get("runs", 0)
    posn = g.get("positive", {}).get("runs", 0)
    negn = est + g.get("negative_unestablished", {}).get("runs", 0)
    notes = []
    na = cid not in APPLIC
    if na:
        notes.append(NA_DESC[cid])
        notes.append(
            "S_not_applicable: groups below come from running the unchanged shim over every spec; S/L=0 is vacuous (no structured tool calls, steps_with_tool_calls=%d of %d steps)."
            % (d["steps_with_tool_calls"], d["steps_total"])
        )
    elif cid == "cuaverifierbench":
        notes.append(
            "_steps carry structured browser-agent tool_calls (Fara-7B actions: left_click, scroll, type, key, visit_url, web_search, terminate ...; arguments dict with action/coordinate/text/url). No file edits or tests/ paths exist in this domain, so S=0 is structural, not an observed clean result. Mapping: none needed (steps passed as-is)."
        )
        notes.append(
            "Positives = incorrect fixed-run outcome (wrong-pass target, not cheating); negatives = strict-majority Correct (registry established)."
        )
    else:
        notes.append(
            "_steps carry structured tool_calls (simulated coding/terminal attack/benign segments, passed as-is, no mapping). Negatives are publisher-constructed benign arms (registry established=43), but they are constructed from attack segments of the same task, so S in a benign arm reflects the publisher's own simulated editing, not an organic clean-editor run."
        )
    if cid == "apollo-insider-trading":
        notes.append(
            "Groups: positive/negative_unestablished = is_misaligned target rows (misalignment phase, 9,600); 'excluded' = 4,823 deception/doubling_down-phase rows (separate out-of-domain targets). Registry counts are for is_misaligned."
        )
    if cid == "hack-ignition":
        notes.append(
            "established = not positive and gold_grades.hardened==1 (passes hardened grader); remaining 7,188 negatives unestablished. Reconciles to registry 5,418."
        )
    if cid == "assistant-axis":
        notes.append(
            "positive = STRONG HACK (157, primary scoring); 32 WEAK HACK are 'excluded' from the primary cohort; 635 weak negatives unestablished (registry established=0)."
        )
    if cid == "collusion-probe-pitfalls":
        notes.append(
            "Groups from labels.state; negatives 3,966 = 1,216 no-brief controls + 2,750 look-alikes, none established (registry established=0)."
        )
    if cid == "automationbench-verified":
        notes.append(
            "positive=defective_verified (210); unchanged_not_certified (590) is negative_unestablished: registry has 0 negatives/0 established."
        )
    if cid == "paichecker":
        notes.append(
            "Negatives = 'No Misalignment' human labels (task-level, registry established 1,502 incl. 227 SWE-bench Multilingual + 1,275 SWE-Gym)."
        )
    rec = (
        (posn, negn, est) == (p, n, e)
        if cid != "automationbench-verified"
        else (posn, 0, 0) == (p, n, e)
    )
    if cid == "automationbench-verified":
        notes.append(
            f"Reconciled on positives (210) and established (0); the registry lists negatives=0 although 590 unchanged_not_certified rows exist (counted as negative_unestablished here)."
        )
    out["corpora"][cid] = {
        "registry": {"positives": p, "negatives": n, "established": e},
        "groups": groups,
        "task_key": TASKKEY[cid],
        "task_keys_distinct": d["task_keys_distinct"],
        "primary_task_key_distinct_all": d["primary_task_key_distinct_all"],
        "S_not_applicable": na,
        "reconciled_to_registry": bool(rec),
        "counts_found": {"positives": posn, "negatives": negn, "established": est},
        "tool_call_structure": {
            "specs_with_steps": d["specs_with_steps"],
            "step_sources": d["step_sources"],
            "steps_with_tool_calls": d["steps_with_tool_calls"],
            "steps_total": d["steps_total"],
        },
        "notes": notes,
        "script": "/private/tmp/np35/scripts/f_run.py %s (then f_assemble.py)" % cid,
    }
json.dump(out, open("/private/tmp/np35/w-f.json", "w"), indent=1)
for cid, c in out["corpora"].items():
    g = c["groups"]
    ne = g.get("negative_established", {})
    print(
        cid,
        "reconciled",
        c["reconciled_to_registry"],
        "NA",
        c["S_not_applicable"],
        "estNeg",
        c["counts_found"]["established"],
        "S_est",
        ne.get("S"),
        "S_tasks",
        ne.get("distinct_tasks_S"),
        "S_pos",
        g.get("positive", {}).get("S"),
    )
