from docent import Docent

c = Docent()
cid = "69be1862-004b-43c2-bd49-e20688d3f965"
q1 = "SELECT COUNT(ar.id) AS n FROM (SELECT id FROM agent_runs) AS ar"
print(c.dql_result_to_dicts(c.execute_dql(cid, q1)))
q2 = """SELECT reward_bucket, COUNT(reward_bucket) AS run_count FROM (
SELECT CASE WHEN CAST(metadata_json->'trace_lab'->>'reward' AS DOUBLE PRECISION) = 1 THEN 'pass'
WHEN CAST(metadata_json->'trace_lab'->>'reward' AS DOUBLE PRECISION) = 0 THEN 'zero'
ELSE 'partial' END AS reward_bucket FROM agent_runs) AS subq GROUP BY reward_bucket"""
print(c.dql_result_to_dicts(c.execute_dql(cid, q2)))
q3 = """SELECT trial_name, msg_count FROM (
SELECT ar.metadata_json->'trace_lab'->>'trial' AS trial_name,
jsonb_array_length(convert_from(t.messages, 'UTF8')::jsonb) AS msg_count
FROM transcripts t JOIN agent_runs ar ON ar.id = t.agent_run_id) AS counted
ORDER BY msg_count DESC LIMIT 6"""
print(c.dql_result_to_dicts(c.execute_dql(cid, q3)))
