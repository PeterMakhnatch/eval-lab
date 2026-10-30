#!/usr/bin/env python3
"""SK-7 attestation: every attempt's stream + session-log route, summarised."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw/SK-7/receipts")


def main() -> int:
    rows = [json.loads(line) for line in (OUT / "attempts.jsonl").read_text().splitlines() if line.strip()]
    recs = []
    for r in rows:
        recs.append({
            "key": f"{r['task_id']}|{r['arm']}|{r['sample']}|{r.get('attempt', 1)}",
            "requested_model": r["requested_model"], "thinking": r.get("thinking"),
            "stream_route": f"{r.get('served_provider_stream')}/{r.get('served_model_stream')}",
            "session_models": r.get("session_models"), "session_assistant_routes": r.get("session_assistant_routes"),
            "fallback_flag_any": r.get("fallback_flag_any"), "session_log_files": r.get("session_log_files"),
            "attested": r.get("attested"), "answer_status": r.get("answer_status"),
            "prompt_sha256": r.get("prompt_sha256"), "raw_response_sha256": r.get("raw_response_sha256"),
            "ts": r.get("ts"),
        })
    attested = sum(1 for r in recs if r["attested"])
    summary = {
        "records": len(recs), "attested": attested, "unattested": len(recs) - attested,
        "observed_stream_routes": sorted({r["stream_route"] for r in recs}),
        "observed_session_models": sorted({m for r in recs for m in (r["session_models"] or [])}),
        "any_fallback_flag": any(r["fallback_flag_any"] for r in recs),
        "unattested_keys": [r["key"] for r in recs if not r["attested"]],
        "records_detail": recs,
    }
    (OUT / "attestation.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"records {len(recs)} attested {attested} unattested {len(recs) - attested} "
          f"routes {summary['observed_stream_routes']} session_models {summary['observed_session_models']} "
          f"fallback {summary['any_fallback_flag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
