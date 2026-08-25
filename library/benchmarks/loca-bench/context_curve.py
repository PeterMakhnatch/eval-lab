"""Emit typed LOCA context-operation facts and query a size curve.

Token counts are measured from generated local MCP state (UTF-8 bytes / 4),
never from padding text. Missing model/scaffold fields remain unknown.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any

def _digest(value: bytes) -> str: return "sha256:" + hashlib.sha256(value).hexdigest()
def _state_bytes(task_dir: Path) -> bytes:
    chunks=[]
    for p in sorted((task_dir / "files").glob("environment_description.json")):
        if p.is_file(): chunks.append(p.read_bytes())
    return b"".join(chunks)

def emit(task_dir: Path, trial_id: str, output: Path) -> list[dict[str, Any]]:
    manifest=json.loads((task_dir / "state_manifest.json").read_text(encoding="utf-8")); content=_state_bytes(task_dir)
    realized=manifest["realized_context_tokens"]; configured=manifest["configured_size_tokens"]
    rows=[{"source_ref":str(task_dir/"state_manifest.json"),"source_digest":_digest((task_dir/"state_manifest.json").read_bytes()),"provenance_kind":"mechanical","trial_id":trial_id,"operation_id":"initial_context","operation":"memory_read","configured_size":configured,"realized_size":realized,"prompt_tokens":manifest["prompt_tokens"],"before_token_count":0,"after_token_count":realized,"content_digest":_digest(content)}]
    # A second event represents a concrete MCP query result; it is not a fabricated
    # model trace and is explicitly marked mechanical in the shared schema.
    rows.append({**rows[0],"operation_id":"mcp_query_result","operation":"compaction","before_token_count":realized,"after_token_count":min(realized,configured),"content_digest":_digest(content[:configured*4])})
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text("\n".join(json.dumps(r,sort_keys=True) for r in rows)+"\n",encoding="utf-8")
    return rows

def project(rows_path: Path, output_dir: Path) -> dict[str, Any]:
    """Project/query the curve through the shared typed ContextOperationFact."""
    from evallab.semantic_facts import ContextOperationFact, project_fact_bundle, query_scorecard
    rows=[ContextOperationFact.model_validate(json.loads(line)) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    bundle={"context_operation_facts":rows}
    project_fact_bundle(bundle, output_dir)
    grouped={}
    for row in rows: grouped.setdefault(row.configured_size, []).append(row)
    curve=[{"configured_size": size, "realized_sizes": sorted({r.realized_size for r in facts if r.realized_size is not None}), "before_tokens": sorted({r.before_token_count for r in facts if r.before_token_count is not None}), "after_tokens": sorted({r.after_token_count for r in facts if r.after_token_count is not None})} for size, facts in sorted(grouped.items())]
    return {"benchmark":"LOCA-bench","curve":curve,"scorecard":query_scorecard(output_dir, benchmark="LOCA-bench")}

if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd",required=True)
    e=sub.add_parser("emit"); e.add_argument("--task-dir",type=Path,required=True); e.add_argument("--trial-id",required=True); e.add_argument("--output",type=Path,required=True)
    q=sub.add_parser("query"); q.add_argument("--rows",type=Path,required=True); q.add_argument("--output-dir",type=Path,required=True)
    a=p.parse_args()
    if a.cmd=="emit": print(json.dumps(emit(a.task_dir,a.trial_id,a.output),sort_keys=True))
    else: print(json.dumps(project(a.rows,a.output_dir),sort_keys=True))
