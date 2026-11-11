"""Separate task, grader, and submission-level cached DEV measurements."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .inputs import identity
from .metrics import clustered_count_rates, rate


def counted(rows: list[tuple[str, int, int]], *, draws: int, seed: int, difference: bool = False) -> dict:
    groups: dict[str, list[tuple[int, int]]] = {}
    for family, k, n in rows:
        old = groups.setdefault(family, [(0, 0)])[0]
        groups[family][0] = (old[0] + k, old[1] + n)
    k, n = sum(k for _, k, _ in rows), sum(n for _, _, n in rows)
    output = {"k": k, "n": n, "rate": k / n if n else None} if difference else rate(k, n)
    output["task_ci95"] = clustered_count_rates(groups, draws=draws, seed=seed)[0] if groups else None
    output["families"] = len(groups)
    output["macro_per_family"] = sum(v[0][0] / v[0][1] for v in groups.values()) / len(groups) if groups else None
    return output


def measure(members: dict[str, str], subjects: dict, scores: dict, *, draws: int, seed: int) -> dict:
    measured = {k: p for k, p in scores.items() if k in members and type(p.get("flag")) is bool}
    output: dict[str, Any] = {"coverage": {"eligible": len(members), "scored": len(measured),
        "missing": len(members) - len(measured), "families": len({subjects[k]["family"] for k in members})}}
    output["observed_alarms"] = counted(
        [(subjects[k]["family"], int(p["flag"]), 1) for k, p in measured.items()], draws=draws, seed=seed)
    output["coverage"]["conflicting"] = sum(p.get("status") == "conflicting" for k, p in scores.items() if k in members)
    for name in ("positive", "strong_negative", "weak_negative", "paired_negative"):
        selected = {k for k, label in members.items() if label == name}
        values = [(subjects[k]["family"], int(measured.get(k, {}).get("flag", False)), 1) for k in sorted(selected)]
        metric = counted(values, draws=draws, seed=seed)
        if selected and not measured:
            metric.update(k=None, rate=None, task_ci95=None, wilson_ci95=[None, None], macro_per_family=None)
        metric["scored"] = len(selected & measured.keys())
        metric["missing"] = len(selected - measured.keys())
        metric["worst_case_alarm"] = rate(sum(measured[k]["flag"] for k in selected & measured.keys())
                                          + metric["missing"], len(selected)) if name != "positive" else None
        output[name] = metric
    costs = {}
    for field in ("cost_usd", "estimated_usd", "tokens_in", "tokens_out", "wall_s", "cpu_s"):
        values = [p[field] for k, p in scores.items() if k in members and p.get(field) is not None]
        costs[field] = {"value": sum(values) if values else None, "recorded": len(values), "eligible": len(members)}
    output["cost"] = costs
    return output


def build(data: dict, config: dict, bootstrap: dict) -> tuple[dict, list[dict]]:
    draws, seed = bootstrap["draws"], bootstrap["seed"]
    subjects, predictions = data["subjects"], data["predictions"]
    rows, receipts = [], []
    for name, panel in data["panels"].items():
        members = panel["members"]
        signature = identity({k: {"label": label, "subject": subjects[k]} for k, label in sorted(members.items())})
        if config["cohorts"][name] != {"sha256": signature, "eligible": len(members)}:
            raise ValueError(f"Frozen QA cohort differs: {name}")
        for tool, spec in data["tools"].items():
            if panel["contract"] not in spec["contracts"]:
                continue
            scores = predictions[tool]
            result = measure(members, subjects, scores, draws=draws, seed=seed)
            rows.append({"cohort": name, "contract": panel["contract"], "label": panel["label"],
                         "cohort_sha256": signature, "tool": tool, "identity": spec["identity"],
                         "role": spec["role"], "note": panel["note"], **result})
            for key, label in sorted(members.items()):
                receipts.append({"contract": panel["contract"], "cohort": name, "tool": tool,
                                 "label": label, **subjects[key],
                                 "prediction": scores.get(key, {"status": "missing", "flag": None})})
    paired = []
    for tool, scores in predictions.items():
        values, strict_flags, original_flags = [], [], []
        for family, pair in data["pairs"].items():
            strict, original = (scores.get(pair[v], {}).get("flag") for v in ("conflicting_certified_bool", "original"))
            if type(strict) is bool and type(original) is bool:
                values.append((family, int(strict) - int(original), 1))
                strict_flags.append((family, int(strict), 1))
                original_flags.append((family, int(original), 1))
        if "task_qa" in data["tools"][tool]["contracts"]:
            paired.append({"tool": tool, "complete_pairs": len(values), "eligible_pairs": len(data["pairs"]),
                           "difference": counted(values, draws=draws, seed=seed, difference=True),
                           "strict_flags": counted(strict_flags, draws=draws, seed=seed),
                           "original_flags": counted(original_flags, draws=draws, seed=seed)})
    for link in data["links"]:
        key = link["task_key"]
        link["tools"] = {contract: {name: predictions[name].get(key, {"status": "missing", "flag": None})
                                   for name, spec in data["tools"].items() if contract in spec["contracts"]}
                         for contract in ("task_qa", "grader_qa")}
        receipts.append({"contract": "gold_run_qa_link", **link})
    receipts.extend({"contract": "grader_submission", "tool": "gold_strict", "native_cell": cell}
                    for cell in data["cells"])
    submission_rows = submissions(data["cells"], draws=draws, seed=seed)
    result = {"rows": rows, "paired": paired, "submissions": submission_rows,
              "cohorts": {name: {**{k: v for k, v in panel.items() if k != "members"},
                                    "counts": dict(Counter(panel["members"].values())),
                                    **config["cohorts"][name]} for name, panel in data["panels"].items()},
              "gold_link": {"executed_positive_runs": len(data["links"]),
                            "unique_task_versions": len({r["task_key"] for r in data["links"]}),
                            "passed_runs": sum(r["passed"] is True for r in data["links"]),
                            "unknown_pass_runs": data["unknown_pass_runs"],
                            "unbound_passed_graders": data["unbound_passed_graders"],
                            "availability": {field: dict(Counter("unknown" if r[field] is None else str(r[field]).lower()
                                                                  for r in data["links"]))
                                             for field in ("task_available", "grader_available")}},
              "limits": config["limits"]}
    return result, receipts


def submissions(cells: list[dict], *, draws: int, seed: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for cell in cells:
        arm = cell["arm"]
        if arm in {"train", "eval"}:
            category = cell["static_category"]
        elif arm == "witness":
            category = cell["control"]
        else:
            category = arm
        groups[category].append(cell)
        if arm in {"witness", "startup"}:
            groups["integrity_all"].append(cell)
    output = []
    for category, rows in sorted(groups.items()):
        usable = [r for r in rows if r["status"] == "executed" and type(r.get("reward")) in (int, float)
                  and r["reward"] in (0, 1) and not r.get("native_exception") and not r.get("reason")]
        counts = [("format-code-task-" + r["task"], int(r["reward"] == 0), 1) for r in usable]
        output.append({"tool": "strict Goldmore repaired graders", "category": category,
                       "unit": "submission cell, not grader", "rejection": counted(counts, draws=draws, seed=seed),
                       "planned": len(rows), "scored": len(usable), "missing": len(rows) - len(usable),
                       "estimated_usd": sum(r["est_usd"] for r in usable) if usable and all(r.get("est_usd") is not None for r in usable) else None,
                       "wall_s": sum(r["wall_s"] for r in usable) if usable and all(r.get("wall_s") is not None for r in usable) else None})
    return output


def render(value: dict) -> list[str]:
    from .board import interval, number, show_rate

    def display(metric: dict) -> str:
        text = show_rate(metric)
        if metric.get("rate") in (0, 1):
            low, high = metric["wilson_ci95"]
            text += f" Wilson [{low:.3f}, {high:.3f}]"
        return text

    lines = ["", "## Task and grader QA (separate DEV contracts)", "",
             "Every cell reports k/n and a task-family clustered 95% interval. Missing outputs never start a tool.",
             "Boundary rates also show Wilson 95% intervals: a collapsed bootstrap is not proof of certainty.",
             "Alarm denominators include missing outputs. Coverage and worst-case alarm bounds remain in JSON.",
             "Evidence-lookup rows reuse label-producing controls. They do not measure independent discovery."]
    for note in value["limits"]:
        lines += ["", note]
    for name, cohort in value["cohorts"].items():
        lines += ["", f"### {name}: {cohort['contract']} / {cohort['label']}", "", cohort["note"], "",
                  f"Eligible subjects: {cohort['eligible']}. Label counts: `{cohort['counts']}`.", "",
                  "| Tool / role | Positive recall | Strong-negative alarms | Weak-negative alarms | Paired-original alarms | Alarms / scored | Scored / eligible; missing | Recorded cost |",
                  "|---|---|---|---|---|---|---|---|"]
        for row in (r for r in value["rows"] if r["cohort"] == name):
            c = row["coverage"]
            costs = row["cost"]["cost_usd"]
            estimated = costs["value"] is None
            if estimated:
                costs = row["cost"]["estimated_usd"]
            text = "unknown" if costs["value"] is None else f"${costs['value']:.4f} ({costs['recorded']}/{costs['eligible']})"
            if estimated and costs["value"] is not None:
                text += " estimated"
            fields = [display(row[k]) + (f"; unresolved {row[k]['missing']}/{row[k]['n']}"
                      if row[k]["missing"] and row[k]["k"] is not None else "")
                      for k in ("positive", "strong_negative", "weak_negative", "paired_negative")]
            lines.append(f"| {row['tool']} / {row['role']} | {' | '.join(fields)} | {display(row['observed_alarms'])} | "
                         f"{c['scored']}/{c['eligible']}; {c['missing']} | {text} |")
    lines += ["", "### Strict/original paired discrimination", "",
              "| Tool | Complete / eligible pairs | Strict alarms | Original alarms | Mean flag(strict) − flag(original) [95% CI] |",
              "|---|---|---|---|---|"]
    for row in value["paired"]:
        delta = row["difference"]
        lines.append(f"| {row['tool']} | {row['complete_pairs']}/{row['eligible_pairs']} | {display(row['strict_flags'])} | "
                     f"{display(row['original_flags'])} | {number(delta['rate'])} {interval(delta['task_ci95'])} |")
    lines += ["", "### Submission-level rejection (never grader counts)", "",
              "Fresh alternatives are not independently proven correct. Their rejection rate is diagnostic, not a semantic false-rejection rate.",
              "POSSIBLY_VALID and historical informed alternatives remain separate from DEFECT and fresh alternatives.", "",
              "| Native cell class | Rejected / usable [95% CI] | Scored / planned; missing | Recorded estimate USD | Recorded wall seconds |",
              "|---|---|---|---|---|"]
    for row in value["submissions"]:
        lines.append(f"| {row['category']} | {display(row['rejection'])} | {row['scored']}/{row['planned']}; {row['missing']} | "
                     f"{number(row['estimated_usd'])} | {number(row['wall_s'])} |")
    link = value["gold_link"]
    lines += ["", "### Executed-positive Gold run → task/grader QA", "",
              f"The external `qa-links.jsonl` contains all {link['executed_positive_runs']} executed-positive runs and every tool output or missing status.",
              f"These runs cover {link['unique_task_versions']} task versions. Recorded passes: {link['passed_runs']}; unknown pass status: {link['unknown_pass_runs']}.",
              f"Passed task versions with unbound grader identity: {link['unbound_passed_graders']}.",
              f"Availability counts: `{link['availability']}`."]
    return lines
