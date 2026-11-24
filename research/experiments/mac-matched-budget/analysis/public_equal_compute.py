import argparse
import json
import math
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from random import Random
from statistics import fmean

POSTS = Path.home() / "Developer/research-context/multi-agent/sources/posts"
MAIN = "2026-11-18-mac-bench-public-result-data.md"
REPORT = "2026-11-18-multi-agent-coordination-bench.md"
SOTA = "2026-11-18-sota-compressor-task-data.md"
SEED_TO_PROFIT = "2026-11-18-seed-to-profit-public-rollout.md"
HOURS = (0.5, 1, 2, 3, 4)
SIZES = (1, 2, 4, 8)
MARKS = {"2": 2, "4": 1, "8": 0, "control8": 0}
SETTINGS = ("1", *MARKS)
SILENT = ("holdout", "dubai-atc", "mindustry")
SEED = 20261124
DRAWS = 10_000
MAX_CAPTURE_BYTES = 8_000_000


def capture_text(path):
    with path.open("rb") as source:
        data = source.read(MAX_CAPTURE_BYTES + 1)
    assert 0 < len(data) <= MAX_CAPTURE_BYTES, path
    return data.decode("utf-8")


def capture_json(path):
    _, start, payload = capture_text(path).partition("```json\n")
    body, end, _ = payload.partition("\n```")
    assert start and end, path
    return json.loads(body)


def score(value):
    assert type(value) in (float, int) and math.isfinite(value), value
    return Fraction(str(value))


def control_type(task):
    return "silent" if task in SILENT else "best_of_k"


def run_cells(main):
    assert tuple(main["overtime"]["hours"]) == HOURS
    assert main["settings"] == [
        "1 agent", "2 agents", "4 agents", "8 agents", "8 agents without coordination"
    ]
    assert len(main["runs"]) == 12 and len(main["models"]) == 3
    rows = {}
    cells = defaultdict(list)
    for task, data in main["runs"].items():
        assert 1 <= len(data["runs"]) <= 64, task
        for index, row in enumerate(data["runs"]):
            model, setting, number, *marks = row
            assert 0 <= model < 3 and 0 <= setting < 5 and 1 <= number <= 8
            assert len(marks) == len(HOURS), (task, index)
            group = task, main["models"][model]["key"], SETTINGS[setting]
            key = (*group, number)
            assert key not in rows, key
            rows[key] = (index, marks)
            cells[group].append([score(value) for value in marks])
    assert len(rows) == 517
    curves = {
        key: (len(runs), [sum(marks) / len(runs) for marks in zip(*runs, strict=True)])
        for key, runs in cells.items()
    }
    return rows, curves


def parity(team, target, k, published):
    index = next((i for i, value in enumerate(team) if value >= target), None)
    lower = 4 if index is None else (HOURS[index - 1] if index else 0)
    upper = None if index is None else HOURS[index]
    assert published is None or 0 <= published <= HOURS[-1], published
    estimate = None
    if index is not None:
        previous = team[index - 1] if index else 0
        estimate = lower
        if previous < target:
            assert team[index] > previous
            estimate += (upper - lower) * float((target - previous) / (team[index] - previous))
    return {
        "wall_hours": [lower, upper],
        "agent_hours": [k * lower, None if upper is None else k * upper],
        "first_sample_hours": upper,
        "publisher_wall_hours": published,
        "publisher_agent_hours": None if published is None else k * published,
        "publisher_in_bracket": (
            published is None if upper is None else published is not None and lower <= published <= upper
        ),
        "linear_check_minutes_error": (
            None if estimate is None or published is None else 60 * abs(estimate - published)
        ),
    }


def cell_results(main, curves):
    clock = main["time"]
    assert clock["k"] == list(SIZES) and clock["budget"] == 4
    assert set(clock["tasks"]) == {data["name"] for data in main["runs"].values()}
    results = []
    for model, time_data in zip(main["models"], clock["models"], strict=True):
        key = model["key"]
        assert model["name"] == time_data["name"] and len(time_data["times"]) == 4
        assert all(len(curve) == 12 for curve in time_data["times"])
        for task, data in main["runs"].items():
            index = clock["tasks"].index(data["name"])
            n, solo = curves[task, key, "1"]
            row = {
                "task": task, "model": key, "control_type": control_type(task),
                "solo_n": n, "solo_scores": solo, "equal_hours": {}, "parity": {},
            }
            for setting, mark in MARKS.items():
                arm = curves.get((task, key, setting))
                assert arm is not None or setting == "control8", (task, key, setting)
                row["equal_hours"][setting] = None if arm is None else {
                    "n": arm[0], "scores": arm[1], "mark_hours": HOURS[mark],
                    "delta": arm[1][mark] - solo[-1],
                }
            for i, k in enumerate(SIZES[1:], 1):
                row["parity"][str(k)] = parity(
                    curves[task, key, str(k)][1], solo[-1], k, time_data["times"][i][index]
                )
            results.append(row)
    assert len(results) == 36
    return results


def equal_summary(rows, setting):
    by_task = defaultdict(list)
    values = []
    for row in rows:
        arm = row["equal_hours"][setting]
        if arm is not None:
            values.append((row["solo_scores"][-1], arm))
            by_task[row["task"]].append(arm["delta"])
    if not values:
        return {"cells": 0, "eligible_cells": len(rows), "mean_delta": None, "interval95": None}
    clusters = [(float(sum(item)), len(item)) for item in by_task.values()]
    interval = bootstrap_interval(clusters) if len(clusters) > 1 else None
    return {
        "cells": len(values), "eligible_cells": len(rows), "tasks": len(clusters),
        "mean_solo": fmean(solo for solo, _ in values),
        "mean_arm": fmean(arm["scores"][MARKS[setting]] for _, arm in values),
        "mean_delta": fmean(arm["delta"] for _, arm in values),
        "interval95": interval,
        "ahead": sum(arm["delta"] > 0 for _, arm in values),
        "tied": sum(arm["delta"] == 0 for _, arm in values),
    }


def bootstrap_interval(clusters):
    assert 2 <= len(clusters) <= 12
    rng = Random(SEED)
    samples = []
    for _ in range(DRAWS):
        totals, counts = zip(*rng.choices(clusters, k=len(clusters)), strict=True)
        samples.append(sum(totals) / sum(counts))
    samples.sort()
    return [samples[DRAWS // 40], samples[DRAWS - 1 - DRAWS // 40]]


def parity_summary(rows, setting):
    data = [row["parity"][setting] for row in rows]
    marks = [row["agent_hours"] for row in data]
    return {
        "cells": len(data),
        "first_mark_before_4": sum(hi is not None and hi < 4 for _, hi in marks),
        "first_mark_at_4": sum(hi == 4 for _, hi in marks),
        "first_mark_after_4": sum(hi is not None and hi > 4 for _, hi in marks),
        "no_mark": sum(hi is None for _, hi in marks),
        "publisher_estimate_before_4": sum(
            (value := row["publisher_agent_hours"]) is not None and value < 4 for row in data
        ),
        "publisher_no_mark": sum(row["publisher_wall_hours"] is None for row in data),
        "publisher_outside_bracket": sum(not row["publisher_in_bracket"] for row in data),
    }


def marginal(final, solo_hour_2, cost):
    assert len(final) == len(cost) == 4
    gains = [b - a for a, b in zip(final[:-1], final[1:], strict=True)]
    increments = [b - a for a, b in zip(cost[:-1], cost[1:], strict=True)]
    assert all(value > 0 for value in increments)
    extra_time_gain = final[0] - solo_hour_2
    return {
        "increments": increments, "final": final, "gains": gains,
        "slope": [gain / delta for gain, delta in zip(gains, increments, strict=True)],
        "solo_hour_2": solo_hour_2, "solo_2_to_4_gain": extra_time_gain,
        "solo_flat_cost_slope_inferred": extra_time_gain / 0.5,
    }


def groups(main, rows):
    subsets = {"all": rows}
    for key in ("model", "control_type", "task"):
        for value in dict.fromkeys(row[key] for row in rows):
            subsets[f"{key}:{value}"] = [row for row in rows if row[key] == value]
    for model in main["models"]:
        key = model["key"]
        for kind in ("silent", "best_of_k"):
            subsets[f"control_type:{kind}:{key}"] = [
                row for row in rows if row["control_type"] == kind and row["model"] == key
            ]
    return subsets


def group_results(subsets, cost):
    output = {}
    for name, group in subsets.items():
        assert group, name
        final = [fmean(row["solo_scores"][-1] for row in group)]
        final.extend(
            fmean(row["equal_hours"][str(k)]["scores"][-1] for row in group) for k in SIZES[1:]
        )
        output[name] = {
            "equal_hours": {setting: equal_summary(group, setting) for setting in MARKS},
            "parity": {str(k): parity_summary(group, str(k)) for k in SIZES[1:]},
            "marginal_shared_cost_axis": marginal(
                final, fmean(row["solo_scores"][2] for row in group), cost
            ),
        }
    return output


def publisher_results(main, cost):
    overtime = {}
    marginal_by_model = {}
    assert len(main["overtime"]["rows"]) == 4 and len(main["bysize"]) == 3
    for row in main["overtime"]["rows"]:
        for setting in ("one", "team", "ctrl"):
            assert len(row[setting]) == len(HOURS)
        one, team, control = row["one"][-1], row["team"][0], row["ctrl"][0]
        overtime[row["key"]] = {
            "solo_4h": one, "team8_30m": team, "control8_30m": control,
            "team8_delta": score(team) - score(one), "control8_delta": score(control) - score(one),
            "control8_4h": row["ctrl"][-1], "solo_hour_2": row["one"][2],
        }
    for model, data, scored in zip(main["models"], main["bysize"], main["scores"], strict=True):
        key = model["key"]
        assert model["name"] == data["name"] == scored["name"]
        assert [point[0] for point in data["pts"]] == list(SIZES)
        assert scored["ctrl"][0] == overtime[key]["control8_4h"]
        final = [score(point[1]) for point in data["pts"]]
        assert final[0] == score(overtime[key]["solo_4h"])
        marginal_by_model[key] = marginal(final, score(overtime[key]["solo_hour_2"]), cost)
    return {"overtime": overtime, "marginal": marginal_by_model, "cost_axis": cost}


def report_costs(path, main, rows):
    task_names = {data["name"]: task for task, data in main["runs"].items()}
    models = {item["name"]: item["key"] for item in main["models"]}
    settings = dict(zip(main["settings"], SETTINGS, strict=True))
    costs = {}
    task = None
    for number, line in enumerate(capture_text(path).splitlines(), 1):
        if line.startswith("## "):
            task = task_names.get(line[3:])
        if task is None or not line.startswith("| "):
            continue
        model, setting, run, final, usd, *_ = [x.strip() for x in line.split("|")[1:-1]]
        if model not in models:
            continue
        key = task, models[model], settings[setting], int(run)
        assert key in rows and key not in costs, (key, number)
        index, marks = rows[key]
        assert abs(score(marks[-1]) - Fraction(final)) <= Fraction(1, 200), key
        assert usd.startswith("$"), key
        dollars = int(usd[1:].replace(",", ""))
        assert dollars > 0, key
        costs[key] = {"usd": dollars, "source": REPORT, "line": number, "chart_index": index}
    assert costs.keys() == rows.keys(), (len(costs), len(rows))
    return costs


def precise_cost(costs, rows, key, marks, usd, source, path, agent_hours=None):
    assert key in costs and marks == rows[key][1], key
    assert usd > 0 and abs(costs[key]["usd"] - usd) <= 0.5, key
    costs[key].update({"usd": usd, "source": source, "json_path": path})
    if agent_hours is not None:
        assert agent_hours > 0
        costs[key]["final_agent_hours"] = agent_hours


def sota_costs(data, costs, rows):
    task = "sota-compressor"
    assert data["ck"] == [60 * hour for hour in HOURS]
    assert set(data["runs"]) == set(data["stats"]) == {"opus", "sol", "argon"}
    oracle = {}
    for model, sizes in data["runs"].items():
        assert set(sizes) == set(SETTINGS[:4])
        for size, runs in sizes.items():
            assert len(runs) == 3
            for index, run in enumerate(runs):
                precise_cost(
                    costs, rows, (task, model, size, run["r"]), run["s"], run["c"], SOTA,
                    f'$.runs.{model}["{size}"][{index}]', run["ah"],
                )
        stats = data["stats"][model]
        assert len(stats["solo"]) == 8 and max(stats["solo"]) == stats["S0"]
        usd = stats["best_of_cost"]["8"]
        assert usd > 0
        oracle[model] = {
            "final_score": stats["S0"], "usd": usd, "score_per_usd": stats["S0"] / usd,
            "scores": stats["ctrl_curve"], "pool_n": len(stats["solo"]),
        }
    return oracle


def seed_costs(data, costs, rows):
    task = "seed-to-profit"
    assert tuple(data["markHours"]) == HOURS
    models = data["tasks"][task]["models"]
    keys = {"opus-5-5": "opus", "gpt-6-1-sol": "sol", "gemini-4-argon": "argon"}
    assert models.keys() == keys.keys()
    for slug, model in keys.items():
        assert len(models[slug]["runs"]) == 15
        for index, run in enumerate(models[slug]["runs"]):
            assert run["setting"] in ("control", "team") and run["teamSize"] in SIZES
            size = "control8" if run["setting"] == "control" else str(run["teamSize"])
            metrics = run["metrics"]
            assert metrics["score"] == metrics["marks"][-1]
            precise_cost(
                costs, rows, (task, model, size, run["rollout"]), metrics["marks"],
                metrics["costUsd"], SEED_TO_PROFIT, f"$.tasks.{task}.models.{slug}.runs[{index}].metrics",
            )


def dollar_results(costs, rows):
    groups = defaultdict(list)
    for (task, model, setting, number), cost in costs.items():
        final = rows[task, model, setting, number][1][-1]
        groups[task, model, setting].append({"run": number, "final_score": final, **cost})
    results = []
    for (task, model, setting), runs in groups.items():
        total_score = sum(score(run["final_score"]) for run in runs)
        total_usd = sum(score(run["usd"]) for run in runs)
        assert total_usd > 0
        results.append({
            "task": task, "model": model, "control_type": control_type(task), "setting": setting,
            "n": len(runs), "mean_final_score": total_score / len(runs),
            "mean_usd": total_usd / len(runs), "score_per_usd": total_score / total_usd,
            "mean_final_agent_hours": (
                fmean(run["final_agent_hours"] for run in runs)
                if all("final_agent_hours" in run for run in runs) else None
            ),
            "runs": runs,
        })
    return results


def dollar_summary(subsets, conditions):
    lookup = {
        (row["task"], row["model"], row["setting"]): row["score_per_usd"] for row in conditions
    }
    output = {}
    for name, group in subsets.items():
        keys = [(row["task"], row["model"]) for row in group]
        output[name] = {}
        for setting in SETTINGS:
            pairs = [
                (lookup[key + ("1",)], lookup[key + (setting,)])
                for key in keys
                if key + (setting,) in lookup
            ]
            output[name][setting] = {
                "cells": len(pairs),
                "mean_score_per_usd": fmean(arm for _, arm in pairs) if pairs else None,
                "ahead_of_solo": sum(arm > solo for solo, arm in pairs),
            }
    return output


def analyze(posts):
    main = capture_json(posts / MAIN)
    rows, curves = run_cells(main)
    cells = cell_results(main, curves)
    costs = report_costs(posts / REPORT, main, rows)
    oracle = sota_costs(capture_json(posts / SOTA), costs, rows)
    seed_costs(capture_json(posts / SEED_TO_PROFIT), costs, rows)
    assert main["cost"]["k"] == list(SIZES)
    cost_axis = [score(value) for value in main["cost"]["cost"]]
    subsets = groups(main, cells)
    conditions = dollar_results(costs, rows)
    return {
        "sources": [MAIN, REPORT, SOTA, SEED_TO_PROFIT], "hours": HOURS, "chart_rows": len(rows),
        "bootstrap": {"unit": "task", "seed": SEED, "draws": DRAWS}, "cells": cells,
        "groups": group_results(subsets, cost_axis), "publisher": publisher_results(main, cost_axis),
        "publisher_task_gains": main["taskgains"],
        "dollars": {
            "spend_at_marks": None, "conditions": conditions,
            "groups": dollar_summary(subsets, conditions),
            "sota_task_page_oracle8": oracle,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Compare captured MAC Bench scores at nominal equal agent-hours.")
    parser.add_argument("--posts", type=Path, default=POSTS, help="folder with the 4 Markdown captures")
    parser.add_argument("--json", action="store_true", help="emit cells, summaries and cost provenance as JSON")
    args = parser.parse_args()
    result = analyze(args.posts)
    if args.json:
        print(json.dumps(result, indent=2, default=float, allow_nan=False))
        return
    for setting, row in result["groups"]["all"]["equal_hours"].items():
        lo, hi = row["interval95"]
        print(f"{setting:>8}: {row['mean_delta']:+.4f} [{lo:+.4f}, {hi:+.4f}], "
              f"ahead {row['ahead']}/{row['cells']} measured cells ({row['eligible_cells']} eligible)")


if __name__ == "__main__":
    main()
