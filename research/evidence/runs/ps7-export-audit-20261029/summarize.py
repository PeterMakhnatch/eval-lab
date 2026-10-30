"""Add counts-only provenance and scaling summaries to the measurement checkpoint."""

import json
import platform
import statistics
from datetime import UTC, datetime
from pathlib import Path

import audit

r = json.loads(audit.RESULT.read_text())
assert r["completed_jobs"] == len(r["jobs"])
assert all(x["selected"] == x["exported"] and x["missing"] == x["extra"] == 0 for x in r["parity"])
assert r["whole_parity"]["selected"] == r["whole_parity"]["exported"] == 4544
assert r["determinism"]["different_files"] == 0
assert all(t["completed"] == t["runs"] for t in r["throughput"])
r["provenance"] = {
    "source_revision": "4fbccb6320c318b0691ae13d27c4c7a76da5ae77",
    "measured_at_utc": datetime.now(UTC).isoformat(),
    "store_bytes": Path(audit.DB).stat().st_size,
    "store_sha256": "06ec014faf70953abe63145c80f9f3ac3f68590989d958ec2517d149bd04ab3e",
    "python": platform.python_version(),
    "platform": platform.system(),
    "read_only": True,
    "batch_limit": 200,
    "cpu_accounting": "os.wait4 on uv CLI child: ru_utime + ru_stime, summed over serial batches",
    "rss_accounting": "Darwin ru_maxrss bytes; maximum child high-water mark, not sum",
    "profiling": "20 independent in-process batches of 100 runs, cProfile; not uninstrumented timings",
}
r["scaling"] = []
for n in (50, 200, 500, 1000, 2000):
    rows = [t for t in r["throughput"] if t["runs"] == n]
    cpu = statistics.median(t["cpu_s"] for t in rows)
    wall = statistics.median(t["wall_s"] for t in rows)
    rss = max(t["peak_rss_bytes"] for t in rows)
    r["scaling"].append(
        {
            "runs": n,
            "repeats": len(rows),
            "median_cpu_s": cpu,
            "median_wall_s": wall,
            "cpu_s_per_100": cpu * 100 / n,
            "runs_per_wall_s": n / wall,
            "peak_rss_bytes": rss,
            "peak_rss_bytes_per_100_runs": rss * 100 / n,
        }
    )
base = r["scaling"][0]["cpu_s_per_100"]
for row in r["scaling"]:
    row["per_run_cpu_growth_vs_50"] = row["cpu_s_per_100"] / base
r["facet_summary"] = {
    "filters": len(r["parity"]),
    "selected_rows_including_overlap": sum(p["selected"] for p in r["parity"]),
    "missing": sum(p["missing"] for p in r["parity"]),
    "extra": sum(p["extra"] for p in r["parity"]),
}
audit.save(r)
print(json.dumps({k: r[k] for k in ("determinism", "scaling", "facet_summary", "exposure")}))
