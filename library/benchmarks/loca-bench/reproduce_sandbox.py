"""Reproduce/audit the pinned LOCA main and sandbox architecture without Harbor edits."""
from __future__ import annotations
import hashlib, json, subprocess
from pathlib import Path
MAIN="8b6fac49d9edd92922593e703b74ea255357c3ec"; SANDBOX="2b4a1c77bd65d83750372ee079a2e5c5d13cb27c"
def sha(data: bytes) -> str: return "sha256:"+hashlib.sha256(data).hexdigest()
def git(repo: Path, *args: str) -> str: return subprocess.check_output(["git","-C",str(repo),*args], text=True).strip()
def reproduce(repo: Path) -> dict:
    main_license=git(repo,"show",f"{MAIN}:LICENSE").encode(); sandbox_license=git(repo,"show",f"{SANDBOX}^:LICENSE").encode()
    files=[]
    for prefix in ("mcp_convert/common/mcp","mcp_convert/mcps/google_cloud","gem/tools/mcp_server/config"):
        files.extend(git(repo,"ls-tree","-r","--name-only",MAIN,"--",prefix).splitlines())
    return {"main_commit":MAIN,"sandbox_commit":SANDBOX,"license":"MIT","main_license_digest":sha(main_license),"sandbox_license_digest":sha(sandbox_license),"license_equal":main_license==sandbox_license,"main_architecture_paths":files,"service_architecture":"stdio BaseMCPServer + local JSON/SQLite GoogleCloudDatabase","sandbox_parent_is_initial":git(repo,"rev-parse",f"{SANDBOX}^") == git(repo,"rev-list","--max-parents=0",SANDBOX),"external_only":True}
if __name__=="__main__":
 import argparse
 p=argparse.ArgumentParser(); p.add_argument("--repo",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); result=reproduce(a.repo); a.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8"); print(json.dumps(result,sort_keys=True))
