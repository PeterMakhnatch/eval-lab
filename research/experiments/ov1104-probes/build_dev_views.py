"""Stand-in DEV views from replay-v1 rollouts in the DetBench view schema.

Used only to build and smoke the extraction pipeline before the detbench lane publishes
/private/tmp/ov1104/shared/detbench/READY. Writes views/<set>/<native_id>.json and views/runs.jsonl.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.expanduser("~/Developer/eval-lab-results/line/replay-v1/inputs")
OUT = "/private/tmp/ov1104/probes/views"
SETS = ["c1c5", "cheatbench", "goodfire", "sol-astra", "terminal-wrench"]
# natural = uninstructed cheating in the source corpus; constructed/prompted corpora get False.
NATURAL = {"c1c5": True, "sol-astra": True, "goodfire": True, "terminal-wrench": None, "cheatbench": False}
LABEL_TYPE = {"c1c5": "executed", "sol-astra": "executed", "goodfire": "dataset", "terminal-wrench": "dataset", "cheatbench": "constructed"}


def native_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:16]


def load_trajectory(path: str) -> tuple[dict, str | None]:
    if os.path.isdir(path):
        for cand in ("trajectory.json", "agent/trajectory.json"):
            p = os.path.join(path, cand)
            if os.path.exists(p):
                diff = None
                dp = os.path.join(path, "final.diff")
                if os.path.exists(dp):
                    with open(dp, errors="replace") as df:
                        diff = df.read()
                with open(p) as pf:
                    return json.load(pf), diff
        return None, None
    with open(path) as pf:
        return json.load(pf), None

def to_view(run_key: str, traj: dict, diff: str | None) -> dict:
    steps = []
    for s in traj["steps"]:
        role = s.get("source") or "agent"
        if role not in ("agent", "tool", "user", "system"):
            role = "agent"
        text_parts = [t for t in (s.get("reasoning_content"), s.get("message")) if t]
        tcs = []
        for tc in s.get("tool_calls") or []:
            args = tc.get("arguments")
            tcs.append({"name": tc.get("function_name") or tc.get("name") or "tool",
                        "args": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
        obs = None
        o = s.get("observation")
        if o and o.get("results"):
            obs = "\n".join(str(r.get("content") or "") for r in o["results"])
        steps.append({"i": len(steps), "role": role, "text": "\n".join(text_parts) or None,
                      "tool_calls": tcs, "observation": obs})
    fm = traj.get("final_message")
    if fm and (not steps or steps[-1]["text"] != fm):
        steps.append({"i": len(steps), "role": "agent", "text": fm, "tool_calls": [], "observation": None})
    return {"run_key": run_key, "task_text": None, "steps": steps, "final": {"reward": None, "diff": diff}}


def main() -> None:
    rows = []
    skipped = []
    for st in SETS:
        with open(f"{ROOT}/corpora/{st}-labels.jsonl") as lf:
            labels = {json.loads(line)["id"]: json.loads(line)["is_cheat"] for line in lf}
        os.makedirs(f"{OUT}/{st}", exist_ok=True)
        with open(f"{ROOT}/corpora/{st}-manifest.jsonl") as mf:
            manifest_lines = list(mf)
        for line in manifest_lines:
            m = json.loads(line)
            nid = native_id(m["id"])
            run_key = f"{st}:{nid}"
            traj, diff = load_trajectory(m["path"])
            if traj is None:
                skipped.append(run_key)
                continue
            view = to_view(run_key, traj, diff)
            vp = f"{OUT}/{st}/{nid}.json"
            with open(vp, "w") as vf:
                json.dump(view, vf, ensure_ascii=False)
            n_chars = sum(len(s["text"] or "") + len(s["observation"] or "") + sum(len(t["args"]) for t in s["tool_calls"]) for s in view["steps"])
            lab = labels[m["id"]]
            rows.append({"run_key": run_key, "set": st, "split": "dev", "label": 1 if str(lab) == "True" else 0,
                         "label_type": LABEL_TYPE[st], "natural": NATURAL[st], "cluster": f"{st}:{m.get('task_id')}",
                         "source_family": st, "view": vp, "n_steps": len(view["steps"]), "n_chars": n_chars})
    with open(f"{OUT}/runs.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    import collections
    print(len(rows), "skipped(no trajectory)", len(skipped), collections.Counter((r["set"], r["label"]) for r in rows))
if __name__ == "__main__":
    sys.exit(main())
