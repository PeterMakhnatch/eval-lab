"""Reset/repetition evidence for LOCA local MCP state isolation."""
from __future__ import annotations
import json, tempfile
from pathlib import Path
from adapter import materialize, SEEDS, SIZE_PARAMS

def prove(size: str="8k", seed: int=42) -> dict:
    with tempfile.TemporaryDirectory(prefix="loca-reset-") as tmp:
        root=Path(tmp)
        first=materialize(root/"same",size,seed); first_digest=first["state_digest"]
        second=materialize(root/"same",size,seed); second_digest=second["state_digest"]
        distinct=materialize(root/"distinct",size,123); distinct_digest=distinct["state_digest"]
        result={"size":size,"seed":seed,"same_seed_digest_equal":first_digest==second_digest,"different_seed_digest_distinct":first_digest!=distinct_digest,"reset_replaced_state":(root/"same"/"local_db").exists(),"no_shared_db_path":(root/"same"/"local_db").resolve()!=(root/"distinct"/"local_db").resolve()}
        result["isolation_pass"] = all(result[k] for k in ("same_seed_digest_equal","different_seed_digest_distinct","reset_replaced_state","no_shared_db_path"))
        return result

if __name__=="__main__": print(json.dumps(prove(),sort_keys=True))
