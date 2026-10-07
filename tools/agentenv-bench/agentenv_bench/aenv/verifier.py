"""Trusted host-side outcome checker using the environment's real SQLite bytes."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
from agentenv_protocol import client

from agentenv_bench.s3k_1591.grader import grade

SYSTEMS = (
    "anaplan_workforce_compensation", "bluesky_approval_workflow",
    "depaul_compensation_audit_register", "workday_hcm",
)


async def verify(mcp_url: str, config: dict) -> list[dict]:
    base = mcp_url.removesuffix("/mcp")
    root = Path(config["run_dir"])
    work = root / "post-work"
    (work / "workspace").mkdir(parents=True, exist_ok=True)
    exports = {}
    for system in SYSTEMS:
        state = await client.get_data(base + "/svc/mcp-" + system)
        if len(state.parts) != 1:
            raise ValueError(f"Expected one DB export for {system}")
        data = state.parts[0].data
        payload = base64.b64decode(data["db_base64"], validate=True)
        digest = hashlib.sha256(payload).hexdigest()
        if data["system"] != system or digest != data["sha256"] or len(payload) != data["size"]:
            raise ValueError("Data-plane state export hash/size mismatch")
        path = work / "system" / system / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        exports[system] = {"path": str(path), "size": len(payload), "sha256": digest}
    report = (root / "final-report.txt").read_text()
    params = json.loads((root / "params.json").read_text())
    rows = grade(work, report, params)
    async with httpx.AsyncClient(trust_env=False) as http:
        trajectory = await http.get(base + "/trajectory")
        trajectory.raise_for_status()
        triggers = await http.get(base + "/triggers/state")
        triggers.raise_for_status()
        (root / "environment-trajectory.jsonl").write_bytes(trajectory.content)
        (root / "trigger-state.json").write_text(json.dumps(triggers.json(), indent=2))
    (root / "grade.json").write_text(json.dumps({"rows": rows, "exports": exports}, indent=2))
    return rows
