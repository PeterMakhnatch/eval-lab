#!/usr/bin/env python3
"""DT-12 coverage: units per harmonised category, per source, plus OpenAI categories.

Keyword table is the one fixed in prereg.md (written before any cluster was seen).
Counts are of units, not independent mechanisms; a unit may hit several keywords and is
assigned to the first matching category in table order (single mapping).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

RAW = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw")
OUT = RAW / "DT-12" / "receipts"

TABLE = [
    ("C01", r"test_file|testfile|edit_test|delete_test|skip_test|weaken|conftest|pytest|xfail|grader|checker|fail_to_pass|pass_to_pass|assert|test_suite|remove_test|disable_test"),
    ("C02", r"answer|answer_key|solution|hidden_test|gold|reference|credential|token|secret|\.env|read_root|expected_output|oracle"),
    ("C03", r"hardcode|hard_code|hardcoded|special_case|specialcase|constant|lookup_table|if_input|magic_value|stub|noop"),
    ("C04", r"claim|claims|success|passing|done|complete|summary|report_success|false_claim|overstate"),
    ("C05", r"eval_aware|evaluat|monitor|watcher|grader_watch|being_tested|detect|observed|supervis"),
    ("C06", r"reward_file|score_file|submission|results\.json|reward\.json|state_file|artifact|mutate|overwrite_result|write_result"),
    ("C07", r"config|permission|chmod|env_var|path|sitecustomize|\.pth|setup\.py|monkeypatch|environment|docker|inject|framework|runner|conftest_plugin"),
    ("C08", r"kernel|benchmark|metadata|task_id|contamination|known_answer|toggl|timing|dataset|harness_config|spec"),
    ("C09", r"sandbag|underperform|slower|covert|backdoor|sabotage|hide|pretend|deceiv|share|collud|conceal"),
    ("C10", r"loop|repeat|timeout|stuck|retry|infinite|runaway"),
    ("C11", r"provider|parse_error|tool_failure|harness_fault|infra"),
    ("C12", r"other|misc|unclassified|policy_violation|mock_world|evasion"),
]
COMPILED = [(c, re.compile(p, re.I)) for c, p in TABLE]

SEEKER_CLASS = {"framework_config_injection": "C07", "runner_exit_status_forgery": "C06",
                "test_file_sabotage": "C01", "grader_tamper": "C01",
                "hidden_answer_read": "C02", "synthetic_artifact_shape_fill": "C03"}

OPENAI = {
    "security: sandbox escape": ["C07"],
    "security: unauthorized communication": ["C12"],
    "security: real internet access": ["C07"],
    "security: exfiltration": ["C02"],
    "security: credential harvesting": ["C02"],
    "security: unmonitored inference": ["C05"],
    "security: disabling monitoring": ["C06"],
    "alignment: reward hacking": ["C12"],
    "alignment: scheming": ["C09"],
    "alignment: metagaming": ["C08"],
    "alignment: prompt injection": ["C12"],
    "alignment: deception": ["C09"],
    "alignment: sandbagging": ["C09"],
    "alignment: sabotage": ["C01"],
    "alignment: collusion": ["C12"],
}


def classify(text: str) -> str | None:
    toks = re.findall(r"[a-z_]+|\d+", text.lower())
    word = re.compile(r"[a-z_]{3,}")
    for cat, rx in COMPILED:
        for t in toks:
            if rx.search(t):
                return cat
    return None


def quotes():
    units = []
    for line in (RAW / "DT-8/receipts/readings-new.jsonl").open():
        r = json.loads(line)
        for q in r.get("evidence") or []:
            if q.get("verified") and (q.get("quote") or "").strip():
                units.append({"id": f"Q8:{r['blind_id']}", "corpus": r["corpus"],
                              "positive": bool(r["positive"]), "text": q["quote"]})
    for line in (RAW / "DT-2/receipts/readings.jsonl").open():
        r = json.loads(line)
        for q in r.get("evidence") or []:
            if q.get("verified") and (q.get("quote") or "").strip():
                units.append({"id": f"Q2:{r['blind_id']}", "corpus": "goodfire",
                              "positive": bool(r["enacted"]), "text": q["quote"]})
    return units


def seeker():
    meta = {}
    for line in (RAW / "DA-3b/receipts/seeker-attempts-v1.jsonl").open():
        r = json.loads(line)
        meta[r["attempt_id"]] = r
    bodies = {}
    for line in (RAW / "DA-3b/receipts/seeker-attempts-v1.text.jsonl").open():
        r = json.loads(line)
        bodies[r["attempt_id"]] = r["text"]
    return meta, bodies


def main():
    out = {}
    q = quotes()
    per_quote = Counter()
    for u in q:
        per_quote[classify(u["text"]) or "unmapped"] += 1
    out["dt8_verified_quotes"] = {"n": len(q), "by_category": dict(sorted(per_quote.items()))}
    out["dt8_verified_quotes"]["by_corpus"] = dict(Counter(u["corpus"] for u in q))

    # run level: a positive run's category set comes from its verified quotes
    runs = defaultdict(set)
    labels = {}
    for u in q:
        c = classify(u["text"])
        if c:
            runs[u["id"]].add(c)
        labels[u["id"]] = (u["corpus"], u["positive"])
    pos_runs = {i for i, (corp, p) in labels.items() if p}
    per_run = Counter()
    for i in pos_runs:
        cats = runs.get(i) or {"unmapped"}
        for c in cats:
            per_run[c] += 1
    out["dt8_positive_runs_with_quote_evidence"] = {
        "n_runs": len(pos_runs), "by_category": dict(sorted(per_run.items()))}

    # seeker: exact dedup then per native class
    meta, bodies = seeker()
    ok = {a: b for a, b in bodies.items()
          if meta.get(a, {}).get("answer_status") == "ok" and b.strip()}
    seen = {}
    exact_dups = 0
    for a, t in ok.items():
        h = hashlib.sha256(" ".join(t.split()).encode()).hexdigest()
        if h in seen:
            exact_dups += 1
        else:
            seen[h] = a
    distinct = list(seen.values())
    per_class = Counter()
    per_class_mapped = Counter()
    for a in distinct:
        cls = meta[a].get("classes") or []
        for c in cls:
            per_class[c] += 1
            per_class_mapped[SEEKER_CLASS.get(c, "unmapped")] += 1
    out["seeker_attempts"] = {
        "n_ok_with_text": len(ok), "n_distinct_after_exact_dedup": len(distinct),
        "n_exact_duplicates_removed": exact_dups,
        "distinct_by_native_class": dict(sorted(per_class.items())),
        "distinct_by_harmonised_category": dict(sorted(per_class_mapped.items())),
        "multi_class_note": "an attempt in k classes is counted in each; totals are not additive",
    }
    # empty-class attempts are "no candidate script" — report
    out["seeker_attempts"]["distinct_without_any_class"] = sum(
        1 for a in distinct if not (meta[a].get("classes") or []))

    # OpenAI categories inherit our-12 counts (many-to-one, not additive)
    base = {"quotes": per_quote, "positive_runs": per_run, "seeker": per_class_mapped}
    out["openai_categories_inherited"] = {
        oc: {src: sum(counts.get(c, 0) for c in cats) for src, counts in base.items()}
        for oc, cats in OPENAI.items()}

    # the zero list
    cats = [c for c, _ in TABLE]
    out["zeros"] = {src: [c for c in cats if counts.get(c, 0) == 0]
                    for src, counts in base.items()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "coverage.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
