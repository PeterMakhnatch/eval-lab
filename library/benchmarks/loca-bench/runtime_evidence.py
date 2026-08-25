"""Record measurable tool-output/refetch/blocked events for an external trial."""
from __future__ import annotations
import json
from pathlib import Path

def record(task_dir: Path, output: Path, *, tool_output_bytes: int|None=None, refetch_events: int|None=0, blocked_events: int|None=0) -> dict:
    manifest=json.loads((task_dir/"state_manifest.json").read_text(encoding="utf-8"))
    payload={"benchmark":"LOCA-bench","trial_state_digest":manifest["state_digest"],"tool_output_bytes":manifest["state_bytes"] if tool_output_bytes is None else tool_output_bytes,"refetch_events":refetch_events,"blocked_events":blocked_events,"event_provenance":"mechanical local adapter observation","unknown_fields":[]}
    output.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return payload
if __name__=="__main__":
 import argparse; p=argparse.ArgumentParser(); p.add_argument("--task-dir",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(record(a.task_dir,a.output),sort_keys=True))
