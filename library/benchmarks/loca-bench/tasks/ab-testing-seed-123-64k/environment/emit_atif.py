"""Emit Harbor-compatible ATIF and the LOCA trial evidence envelope."""
from __future__ import annotations
import hashlib, json
from pathlib import Path

def emit(task_dir: Path, trial_dir: Path, *, scaffold: str="loca-external-adapter", model: str|None=None) -> dict:
    task_dir=task_dir.resolve(); trial_dir=trial_dir.resolve(); manifest=json.loads((task_dir/"state_manifest.json").read_text(encoding="utf-8")); source=task_dir/"state_manifest.json"; digest="sha256:"+hashlib.sha256(source.read_bytes()).hexdigest()
    atif={"schema_version":"ATIF-v1.7","session_id":manifest["state_digest"][:32],"agent":{"name":scaffold,"version":"external-1"},"steps":[
      {"step_id":1,"source":"agent","message":"Materialized pinned LOCA local state","tool_calls":[{"tool_call_id":"loca-prepare","function_name":"prepare_state","arguments":{"size":manifest["size"],"seed":manifest["official_seed"]}}],"observation":{"results":[{"source_call_id":"loca-prepare","content":"state_manifest.json","extra":{"state_digest":manifest["state_digest"]}}]}},
      {"step_id":2,"source":"agent","message":"Queried local Google Cloud MCP database","tool_calls":[{"tool_call_id":"loca-query","function_name":"bigquery_run_query","arguments":{"dataset":"ab_testing","table":"clickstream"}}],"observation":{"results":[{"source_call_id":"loca-query","content":"clickstream state queried","extra":{"row_count":manifest["row_count"],"tool_output_bytes":manifest["realized_state_bytes"]}}]}},
      {"step_id":3,"source":"verifier","message":"Checked deterministic final state","tool_calls":[],"observation":{"results":[{"source_call_id":"loca-query","content":"verifier evidence is in verifier output","extra":{}}]}}
    ]}
    agent_dir=trial_dir/"agent"; agent_dir.mkdir(parents=True,exist_ok=True); (agent_dir/"trajectory.json").write_text(json.dumps(atif,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    envelope={"benchmark":"LOCA-bench","benchmark_version":manifest["benchmark_version"],"upstream_commit":"8b6fac49d9edd92922593e703b74ea255357c3ec","dataset_revision":"final_"+manifest["size"]+"_set_config.json","task_digest":digest,"image_digest":None,"verifier_digest":None,"license":"MIT","model":model,"agent":scaffold,"harness":"external-loca-adapter","harness_version":"1","reasoning":"model identity unavailable for local evidence control","seed":manifest["official_seed"],"repetition":1,"backend":"native-m4","raw_atif_path":"agent/trajectory.json","raw_atif_digest":"sha256:"+hashlib.sha256(json.dumps(atif,sort_keys=True).encode()).hexdigest(),"infra_status":"ok","agent_status":"control","initial_state_digest":manifest["state_digest"],"final_state_digest":manifest["state_digest"],"verifier_assertions":None,"reward_components":None,"evidence_completeness":"mechanical-control-only","runtime_events_path":"runtime_evidence.json"}
    (trial_dir/"trial_evidence.json").write_text(json.dumps(envelope,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return envelope
if __name__=="__main__":
 import argparse; p=argparse.ArgumentParser(); p.add_argument("--task-dir",type=Path,required=True); p.add_argument("--trial-dir",type=Path,required=True); a=p.parse_args(); print(json.dumps(emit(a.task_dir,a.trial_dir),sort_keys=True))
