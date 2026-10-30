"""Append a complete pair, including evidence replies, without repeating throughput."""

import json

import audit

r = json.loads(audit.RESULT.read_text())
r["determinism_raw_with_replies"] = r["determinism"]
r["determinism"] = {
    "runs": 500,
    "compared_files": 0,
    "different_files": 0,
    "reverse_order_required": False,
    "includes_evidence_replies": True,
    "normalized_fields": ["meta.latency_ms"],
}
for repeat in range(2):
    for offset in range(0, 500, 200):
        r["jobs"].append({"kind": "determinism", "repeat": repeat, "offset": offset})
audit.save(r)
