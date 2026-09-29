"""Fetch SWE-bench Verified gold patches (instance_id, patch, base_commit) -> cache JSON."""
import json

from datasets import load_dataset

ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
print("rows:", len(ds), "cols:", ds.column_names)
out = {}
for r in ds:
    out[r["instance_id"]] = {"patch": r["patch"], "base_commit": r["base_commit"]}
with open("/Users/petermakhnatch/.cache/trace-lab/gold_patches.json", "w") as f:
    json.dump(out, f)
print("saved", len(out))
