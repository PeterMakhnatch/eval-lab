"""Download *.traj.json for both submissions; skip files >50MB (record them)."""
import json
import os

import boto3
from botocore import UNSIGNED
from botocore.config import Config

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1")
BUCKET = "swe-bench-submissions"
CACHE = "/Users/petermakhnatch/.cache/trace-lab"
CAP = 50 * 1024 * 1024

JOBS = [
    ("bash-only/20250822_mini-v1.9.1_glm-4.5/trajs/", f"{CACHE}/trajs_glm45"),
    ("bash-only/20260217_mini-v2.0.0_glm-5-high/trajs", f"{CACHE}/trajs_glm5high"),
]

manifest: dict = {"jobs": {}, "skipped_over_cap": []}
for prefix, dest in JOBS:
    os.makedirs(dest, exist_ok=True)
    keys = []
    tok = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            k = o["Key"]
            if k.endswith(".traj.json"):
                keys.append((k, o["Size"]))
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    print(f"{prefix}: {len(keys)} traj files")
    skipped = []
    done = 0
    dl_bytes = 0
    for k, sz in sorted(keys):
        rel = k[len(prefix):].strip("/")
        local = os.path.join(dest, rel)
        if os.path.exists(local) and os.path.getsize(local) == sz:
            done += 1
            dl_bytes += sz
            continue
        if sz > CAP:
            skipped.append({"key": k, "bytes": sz})
            continue
        os.makedirs(os.path.dirname(local), exist_ok=True)
        s3.download_file(BUCKET, k, local)
        done += 1
        dl_bytes += sz
        if done % 100 == 0:
            print(f"  {dest}: {done}/{len(keys)} ({dl_bytes/2**20:.0f} MiB)")
    print(f"  {dest}: downloaded/verified {done}/{len(keys)} ({dl_bytes/2**20:.1f} MiB)")
    for s in skipped:
        print(f"  SKIPPED (>50MB): {s['bytes']} {s['key']}")
    manifest["jobs"][dest] = {"prefix": prefix, "n_traj": len(keys), "done": done}
    manifest["skipped_over_cap"].extend(skipped)

with open(f"{CACHE}/download_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=1)
print("wrote download_manifest.json")
