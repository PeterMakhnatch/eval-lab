from docent import Docent

c = Docent()
cid = "69be1862-004b-43c2-bd49-e20688d3f965"
RID = "024527be-daae-4648-a438-fc5a622bb475"
q = """SELECT CAST(SUM(intok) AS DOUBLE PRECISION) AS tot_in,
CAST(AVG(intok) AS DOUBLE PRECISION) AS avg_in,
CAST(MAX(intok) AS DOUBLE PRECISION) AS max_in,
CAST(SUM(outtok) AS DOUBLE PRECISION) AS tot_out FROM (
SELECT rr.input_tokens AS intok, rr.output_tokens AS outtok
FROM reading_results rr JOIN reading_result_links rrl ON rrl.result_id = rr.id
WHERE rrl.reading_id = 'RID') AS subq""".replace("RID", RID)
print(c.dql_result_to_dicts(c.execute_dql(cid, q)))
info = c.get_collection(cid)
print({k: info.get(k) for k in ("name", "id", "created_by", "created_at")})
print("collabs:", [(x.get("subject_type"), (x.get("subject") or {}).get("email"), x.get("permission_level")) for x in c.get_collection_collaborators(cid)])
