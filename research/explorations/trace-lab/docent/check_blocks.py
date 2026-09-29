
from docent import Docent

c = Docent()
cid = "69be1862-004b-43c2-bd49-e20688d3f965"
q = """SELECT t.id AS tid, t.name AS tname,
jsonb_array_length(convert_from(t.messages, 'UTF8')::jsonb) AS nblocks,
ar.metadata_json->'trace_lab'->>'trial' AS trial_name
FROM transcripts t JOIN agent_runs ar ON ar.id = t.agent_run_id
ORDER BY nblocks DESC LIMIT 3"""
for row in c.dql_result_to_dicts(c.execute_dql(cid, q)):
    print(row)
