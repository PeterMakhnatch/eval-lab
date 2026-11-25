import json
import sys
from pathlib import Path

WORLD_DIR = Path("/world")
if WORLD_DIR.is_dir():
    sys.path.insert(0, str(WORLD_DIR))

from world import KINDS, Desk, case_question, kilogram_total, question_scope  # noqa: E402
from worldkit import close_world, load_events, write_rewards  # noqa: E402


def reconstruct(events, instance):
    desk = Desk(instance)
    for event in events:
        kind = event["kind"]
        if kind == "admin.close" and event["ok"]:
            break
        if not event["ok"]:
            desk.measures["refused_for_budget"] += event["status"] == 429
            continue
        if event["seat"] != "p1":
            continue
        if kind in ("ask", "correct", "file") and event["method"] == "POST":
            getattr(desk, kind)(event["request"])
        elif kind == "records" and event["method"] == "GET":
            desk.records()
    return desk


def replay(events, instance):
    return reconstruct(events, instance).state()


def score(events, instance):
    desk = reconstruct(events, instance)
    right = set()
    for cid, case in desk.cases.items():
        scope = question_scope(instance, case_question(instance, case))
        if desk.files.get(cid) == kilogram_total(instance, scope):
            right.add(cid)
    reward = len(right) / len(desk.cases)
    result = {"reward": reward, "exact": float(reward == 1.0),
              "unfiled": float(len(desk.cases) - len(desk.files)),
              **{key: float(value) for key, value in desk.measures.items()}}
    for kind in KINDS:
        cases = {cid for cid, first in instance["faults"].items() if first == kind}
        result[f"n.{kind}"] = float(len(cases))
        if cases:
            result[f"right.{kind}"] = len(cases & right) / len(cases)
    return result


def main():
    close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text())
    rewards = score(load_events(), instance)
    write_rewards(rewards)
    print(json.dumps(rewards, sort_keys=True))


if __name__ == "__main__":
    main()
