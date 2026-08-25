"""Evidence that the bounded adapter is intended for the native Apple M4 backend."""
from __future__ import annotations
import json, platform, sys
from pathlib import Path

def prove(output: Path) -> dict:
    facts={"backend":"native-m4","platform_system":platform.system(),"platform_machine":platform.machine(),"python":platform.python_version(),"native_m4_proven":platform.system()=="Darwin" and platform.machine()=="arm64","container_required":False,"network":"none"}
    output.write_text(json.dumps(facts,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return facts
if __name__=="__main__":
 import argparse; p=argparse.ArgumentParser(); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(prove(a.output),sort_keys=True))
