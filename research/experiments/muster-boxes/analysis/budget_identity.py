import json
import os
import sys
from pathlib import Path
from statistics import fmean

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("runs/e1")
ARMS = ["solo-a", "solo-x3", "indep-3", "indep-3-salt", "indep-3-mixed", "team-3"]
FROZEN_BUILD = Path(__file__).resolve().parents[4] / "muster" / "tasks" / "boxes" / "build"


def task_dir(recorded: Path) -> Path:
    if recorded.is_dir():
        return recorded
    frozen = FROZEN_BUILD / recorded.parent.name / recorded.name
    assert frozen.is_dir(), recorded
    return frozen


def trials(arm):
    for dirpath, dirnames, filenames in os.walk(ROOT / arm):
        if "result.json" in filenames:
            r = json.loads((Path(dirpath) / "result.json").read_text())
            if "trial_name" in r:
                dirnames[:] = []
                yield Path(dirpath), r


def analyze(path, result):
    log = [
        json.loads(line)
        for line in (path / "verifier" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    played = []
    for e in log:
        if e["kind"] == "admin.close" and e["ok"]:
            break
        if e["seat"] is not None:
            played.append(e)
    task = task_dir(Path(result["config"]["task"]["path"]))
    inst = json.loads((task / "environment" / "world" / "instance.json").read_text())
    budget = inst["budget"] * len(inst["seats"])
    opened = {}
    fetched = {}
    posts = []
    out = dict(
        new_flag=0,
        repeat=0,
        repeat_own=0,
        rep_informed=0,
        rep_inflight=0,
        rep_notsent=0,
        new_empty=0,
        opens=0,
        first_post_open_index=None,
        opens_before_any_fetch=0,
    )
    fetched_any = set()
    for e in played:
        if e["kind"] == "msg" and e["ok"] and e["method"] == "POST":
            posts.append((e["seq"], e["t"], e["seat"], e["response"]["id"]))
        elif e["kind"] == "msgs" and e["ok"]:
            fetched_any.add(e["seat"])
            seen = fetched.setdefault(e["seat"], {})
            for m in e["response"].get("messages", []):
                seen.setdefault(m["id"], e["t"])
        elif e["kind"] == "open" and e["ok"]:
            out["opens"] += 1
            if inst["channel"] and e["seat"] not in fetched_any:
                out["opens_before_any_fetch"] += 1
            box = e["request"]["box"]
            if box in opened:
                out["repeat"] += 1
                first_seat, first_seq = opened[box]
                if first_seat == e["seat"]:
                    out["repeat_own"] += 1
                else:
                    reports = [p for p in posts if p[2] == first_seat and p[0] > first_seq]
                    seen = fetched.get(e["seat"], {})
                    if any(p[3] in seen and seen[p[3]] <= e["t"] for p in reports):
                        out["rep_informed"] += 1
                    elif reports:
                        out["rep_inflight"] += 1
                    else:
                        out["rep_notsent"] += 1
            else:
                opened[box] = (e["seat"], e["seq"])
                if e["response"].get("flag"):
                    out["new_flag"] += 1
                else:
                    out["new_empty"] += 1
    out["unused"] = budget - out["opens"]
    out["budget"] = budget
    out["reward"] = result["verifier_result"]["rewards"]["reward"]
    return out


rows = {}
for arm in ARMS:
    data = [analyze(p, r) for p, r in trials(arm)]
    rows[arm] = {k: fmean(d[k] for d in data) for k in data[0] if data[0][k] is not None} | {"n": len(data)}
cols = [
    "n",
    "budget",
    "reward",
    "new_flag",
    "new_empty",
    "repeat",
    "repeat_own",
    "rep_notsent",
    "rep_inflight",
    "rep_informed",
    "unused",
    "opens_before_any_fetch",
]
print("arm            " + " ".join(f"{c[:12]:>12s}" for c in cols))
for arm, row in rows.items():
    print(f"{arm:14s} " + " ".join(f"{row.get(c, 0):12.2f}" for c in cols))
