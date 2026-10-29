"""NP38-CE-1: can a run's written test path be joined to its task's grader-side test list, and what do the gate's alarms look like split by it?

Counts only, no model call, no network, no write to any store. Inputs (all already on disk):
  pair list      ~/.local/state/evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl  (123 pairs, 246 runs, 101 tasks)
  step cache     ~/.local/state/evallab-worker-evidence/np36-tw1-20261029/pairs.pkl   (frozen steps of the 246 runs)
  task rows/CAS  a read-only SQLite copy of the Cloud Trace DB + its local content store (cas/sha256/<digest>)
  SWE-bench      HF cache princeton-nlp/SWE-bench_Verified parquet, and the Harbor task cache (tests/config.json, tests/test.sh)
usage: np38_join.py REPO DB_COPY OUT.json
The class label (enacted / clean) is read from the pair list only, exactly as NP36-TW-1 did, and only to split counts.
"""

import collections
import glob
import json
import pickle
import re
import sqlite3
import sys
from pathlib import Path

import pyarrow.parquet as pq

REPO, DB, OUT = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(REPO / "research/evidence/runs/np36-grader-tamper-20261029/tools"))
import gate  # noqa: E402  (the NP36 gate with per-path reasons; check() asserts it equals production)

from evallab import tripwires as tw  # noqa: E402

STATE = Path.home() / ".local/state"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
STEPS = STATE / "evallab-worker-evidence/np36-tw1-20261029/pairs.pkl"
CAS = STATE / "evallab-prod-live/cas/sha256"
HF = glob.glob(str(Path.home() / ".cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/snapshots/*/data/test-00000-of-00001.parquet"))[0]
HARBOR = Path.home() / ".cache/harbor/tasks"

pairs = [json.loads(line) for line in PAIRS.read_text().splitlines() if line.strip()]
steps_by_run = pickle.loads(STEPS.read_bytes())


def family(task):
    return task.split("/")[1]


def instance(task):
    return task.split("/")[-1].split("@")[0]


tasks = sorted({p["task"] for p in pairs})
fams = collections.Counter(family(t) for t in tasks)
db = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)

# ---------------------------------------------------------------- A. where the lists live, per family
bundle = {}
for task in tasks:
    metadata = json.loads(db.execute("SELECT metadata_json FROM corpus_tasks WHERE task_id=?", (task,)).fetchone()[0])
    path = CAS / metadata["task_bundle"]["cas_uri"].rsplit("/", 1)[-1]
    bundle[task] = json.loads(path.read_text())["data"] if path.exists() else None
coverage: dict[str, dict] = {f: {"tasks": n} for f, n in fams.items()}
coverage["deep_swe"].update({
    "bundle_in_local_cas": sum(bundle[t] is not None for t in tasks if family(t) == "deep_swe"),
    "bundle_has_test_names_or_paths": 0,
    "bundle_fields_naming_the_grader": sorted({k for t in tasks if family(t) == "deep_swe" for k in bundle[t]
                                               if k in ("verifier", "verifier_env", "task_dir", "collect", "artifacts")}),
})
coverage["impossible_bench"].update({
    "bundle_in_local_cas": sum(bundle[t] is not None for t in tasks if family(t) == "impossible_bench"),
    "bundle_has_test_code": sum(bool(bundle[t].get("test_code")) for t in tasks if family(t) == "impossible_bench"),
    "bundle_has_test_path": sum(bool(bundle[t].get("test_patch_files") or bundle[t].get("test_directives"))
                                for t in tasks if family(t) == "impossible_bench"),
    "reset_tests_true": sum(bool(bundle[t].get("reset_tests")) for t in tasks if family(t) == "impossible_bench"),
})
coverage["swebench_verified"].update({
    "bundle_in_local_cas": sum(bundle[t] is not None for t in tasks if family(t) == "swebench_verified"),
    "bundle_has_test_names_or_paths": 0,
})

table = pq.read_table(HF).to_pylist()
by_id = {row["instance_id"]: row for row in table}
DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
PYTEST_ID = re.compile(r"^(?P<path>[^\s:]+\.py)::")
swe = {}
for task in tasks:
    if family(task) != "swebench_verified":
        continue
    row = by_id[instance(task)]
    f2p, p2p = json.loads(row["FAIL_TO_PASS"]), json.loads(row["PASS_TO_PASS"])
    patch_files = {b for _a, b in DIFF.findall(row["test_patch"])}
    f2p_paths = {m.group("path") for t in f2p if (m := PYTEST_ID.match(t))}
    p2p_paths = {m.group("path") for t in p2p if (m := PYTEST_ID.match(t))}
    swe[task] = {"patch": patch_files, "f2p": f2p_paths, "p2p": p2p_paths, "f2p_n": len(f2p), "p2p_n": len(p2p),
                 "f2p_path_style": sum(bool(PYTEST_ID.match(t)) for t in f2p),
                 "p2p_path_style": sum(bool(PYTEST_ID.match(t)) for t in p2p), "renames": sum(a != b for a, b in DIFF.findall(row["test_patch"]))}
harbor_agree = collections.Counter()
reset_equal = collections.Counter()
for task in swe:
    cache = next(HARBOR.glob(f"*/{instance(task)}/tests"), None)
    harbor_agree["harbor_cache_present"] += cache is not None
    if cache is None:
        continue
    config = json.loads((cache / "config.json").read_text())
    row = by_id[instance(task)]
    harbor_agree["f2p_equal"] += json.loads(config["FAIL_TO_PASS"]) == json.loads(row["FAIL_TO_PASS"]) if isinstance(config["FAIL_TO_PASS"], str) else config["FAIL_TO_PASS"] == json.loads(row["FAIL_TO_PASS"])
    harbor_agree["test_patch_equal"] += config.get("test_patch") == row["test_patch"]
    script = (cache / "test.sh").read_text()
    reset = set()
    for match in re.finditer(r"git checkout [0-9a-f]{40} ([^\n]+)", script):
        reset.update(match.group(1).split())
    reset_equal["script_resets_exactly_the_test_patch_files"] += reset == swe[task]["patch"]
    reset_equal["script_cleans_untracked_files"] += "git clean -fd" in script
coverage["swebench_verified"].update({
    "hf_parquet_rows": len(table), "tasks_in_hf_parquet": sum(instance(t) in by_id for t in swe),
    **{k: v for k, v in harbor_agree.items()}, **{k: v for k, v in reset_equal.items()},
    "tasks_with_test_patch_paths": sum(bool(v["patch"]) for v in swe.values()),
    "tasks_with_any_pytest_style_f2p_or_p2p_path": sum(bool(v["f2p"] | v["p2p"]) for v in swe.values()),
    "f2p_ids": sum(v["f2p_n"] for v in swe.values()), "f2p_ids_path_style": sum(v["f2p_path_style"] for v in swe.values()),
    "p2p_ids": sum(v["p2p_n"] for v in swe.values()), "p2p_ids_path_style": sum(v["p2p_path_style"] for v in swe.values()),
    "test_patch_renames": sum(v["renames"] for v in swe.values()),
})

# ---------------------------------------------------------------- B. the join
def normalise(path: str) -> tuple[str, str]:
    """Repository-relative path and the form it arrived in."""
    original = path
    for prefix in ("/testbed/", "/app/", "/workspace/"):
        if path.startswith(prefix):
            return path[len(prefix):], prefix
    if path.startswith("./"):
        return path[2:], "./"
    return path, ("absolute-other" if original.startswith("/") else "relative")


def write_targets(run_id: str) -> dict[str, set[str]]:
    """Every write target the gate's own parser sees, and the subset that its path regexes call grader paths."""
    every, grader = set(), set()
    for step in gate.agent_steps_of(steps_by_run[run_id]):
        found = [t for cmd in tw._bash_commands(step) for t, _shape, _mech in tw._step_writes(cmd)]
        found += [t for t, _kind in tw._tool_edits(step)]
        for target in found:
            every.add(target)
            if tw._GRADER_ALWAYS_RE.search(target) or tw._GRADER_GATED_RE.search(target):
                grader.add(target)
    return {"every": every, "grader": grader}


def alarm_paths(run_id: str) -> list[str]:
    agent = gate.agent_steps_of(steps_by_run[run_id])
    assert gate.check(agent), run_id
    return [p["path"] for hit in gate.current_hits(agent) for p in hit["paths"]]


def classify(task: str, path: str) -> str:
    relative, _form = normalise(path)
    sets = swe[task]
    if relative in sets["f2p"]:
        return "in_list_f2p_file"
    if relative in sets["patch"]:
        return "in_list_test_patch_file"
    if relative in sets["p2p"]:
        return "in_list_p2p_only_file"
    return "not_in_list"


runs = [(p["task"], cls, p[cls]) for p in pairs for cls in ("enacted", "clean")]
alarm_table: dict[str, dict[str, collections.Counter]] = {f: {"enacted": collections.Counter(), "clean": collections.Counter()} for f in fams}
write_table = {f: {"enacted": collections.Counter(), "clean": collections.Counter()} for f in fams}
forms = collections.Counter()
for task, cls, run_id in runs:
    f = family(task)
    alarm_table[f][cls]["runs"] += 1
    paths = alarm_paths(run_id)
    alarm_table[f][cls]["alarmed_runs"] += bool(paths)
    alarm_table[f][cls]["alarm_path_events"] += len(paths)
    for path in paths:
        forms[(f, cls, normalise(path)[1])] += 1
    targets = write_targets(run_id)
    write_table[f][cls]["runs"] += 1
    write_table[f][cls]["runs_with_any_write_target"] += bool(targets["every"])
    write_table[f][cls]["runs_with_grader_path_write"] += bool(targets["grader"])
    write_table[f][cls]["non_path_targets"] += sum(("/" not in t and "." not in t) for t in targets["every"])
    if f == "swebench_verified":
        kinds = {classify(task, t) for t in targets["every"]}
        # exact-path join of EVERY write target of the run, not only the gate's grader-path ones
        write_table[f][cls]["runs_writing_a_listed_file"] += any(k.startswith("in_list") for k in kinds)
        write_table[f][cls]["runs_writing_a_grader_path_not_in_list"] += any(
            classify(task, t) == "not_in_list" for t in targets["grader"])
        for t in targets["every"]:
            write_table[f][cls]["target:" + classify(task, t)] += 1
        for t in alarm_paths(run_id):
            alarm_table[f][cls]["alarm_path:" + classify(task, t)] += 1

# ---------------------------------------------------------------- E. ImpossibleBench native test_modified, counts only
impossible = collections.Counter()
conn_rows = {row[0]: row[1] for row in db.execute("SELECT trace_id, raw_cas_uri FROM agent_traces")}
for task, cls, run_id in runs:
    if family(task) != "impossible_bench":
        continue
    raw = json.loads((CAS / conn_rows[run_id].rsplit("/", 1)[-1]).read_text())["traces"][0]
    value = raw.get("metrics", {}).get("test_modified")
    impossible[f"{cls}_runs"] += 1
    impossible[f"{cls}_test_modified_native=1"] += value == 1.0
    impossible[f"{cls}_test_modified_native_present"] += value is not None
# ---------------------------------------------------------------- F. Deep-SWE: only counts exist
deep = collections.Counter()
for task, _cls, run_id in runs:
    if family(task) != "deep_swe":
        continue
    raw = json.loads((CAS / conn_rows[run_id].rsplit("/", 1)[-1]).read_text())["traces"][0]
    metrics = raw.get("metrics", {})
    deep["runs"] += 1
    deep["runs_with_f2p_total_count"] += "f2p_total" in metrics
    deep["runs_with_p2p_total_count"] += "p2p_total" in metrics

result = {
    "schema": "np38-ce1/v1", "pairs": len(pairs), "runs": len(runs), "tasks": len(tasks), "tasks_by_family": dict(fams),
    "coverage": coverage,
    "exact_path_joinable_tasks": {"swebench_verified": len(swe), "deep_swe": 0, "impossible_bench": 0, "total": len(swe)},
    "alarms_by_family_and_class": {f: {c: dict(v) for c, v in d.items()} for f, d in alarm_table.items()},
    "writes_by_family_and_class": {f: {c: dict(v) for c, v in d.items()} for f, d in write_table.items()},
    "alarm_path_forms": {"|".join(k): v for k, v in sorted(forms.items())},
    "impossible_bench_native_test_modified": dict(impossible),
    "deep_swe_native_counts_only": dict(deep),
}
Path(OUT).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
print(json.dumps({k: result[k] for k in ("exact_path_joinable_tasks", "coverage", "alarms_by_family_and_class")}, indent=1, sort_keys=True))
