import json
from pathlib import Path

from docent import Docent

c = Docent()
cid = "69be1862-004b-43c2-bd49-e20688d3f965"
RID = "024527be-daae-4648-a438-fc5a622bb475"
q = f"""SELECT trial_name, out_json, err_json FROM (
SELECT CAST(ar.id AS TEXT) AS arid,
ar.metadata_json->'trace_lab'->>'trial' AS trial_name,
rr.output AS out_json, rr.error AS err_json,
rr.input_tokens AS intok, rr.output_tokens AS outtok
FROM reading_results rr
JOIN reading_result_links rrl ON rrl.result_id = rr.id
JOIN agent_runs ar ON CAST(ar.id AS TEXT) = rr.arguments_dict->'run'->>'id'
WHERE rrl.reading_id = '{RID}') AS subq ORDER BY trial_name"""
rows = c.dql_result_to_dicts(c.execute_dql(cid, q))
print("rows:", len(rows))
out = []
for r in rows:
    o = r["out_json"]
    if isinstance(o, str):
        o = json.loads(o)
    out.append({"trial": r["trial_name"], "output": o, "error": r["err_json"]})
with open(Path(__file__).with_name("har81_reading_results.json"), "w") as f:
    json.dump(out, f, indent=1)
tok = sum((r["out_json"] or {}).get("input_tokens", 0) for r in rows if False)
print("wrote har81_reading_results.json")
print(json.dumps(out[0]["output"], indent=1)[:2500])
