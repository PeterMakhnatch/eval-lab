-- End-to-end bounded report: source projection + control/evidence readiness.
-- Unknown trial facts stay NULL; no retrieval recall or reward is inferred.
WITH projection_doc AS (
  SELECT json(content) AS payload
  FROM read_text('library/benchmarks/tau-knowledge/semantic-projection.json')
), projection AS (
  SELECT
    json_extract_string(item.value, '$.task_id') AS task_id,
    json_extract_string(item.value, '$.construct') AS construct,
    json_extract_string(item.value, '$.criteria_digest') AS criteria_digest,
    CAST(json_extract_string(item.value, '$.observed') AS BOOLEAN) AS observed
  FROM projection_doc, json_each(json_extract(payload, '$.rows')) AS item
), attempts_doc AS (
  SELECT json(content) AS payload
  FROM read_text('library/benchmarks/tau-knowledge/evidence/control-attempts.json')
), readiness AS (
  SELECT
    json_extract_string(payload, '$.benchmark') AS benchmark,
    json_extract_string(payload, '$.analysis_status') AS analysis_status,
    CAST(json_extract_string(payload, '$.luna_attempted') AS BOOLEAN) AS luna_attempted
  FROM attempts_doc
)
SELECT
  readiness.benchmark,
  projection.task_id,
  projection.construct,
  projection.criteria_digest,
  projection.observed,
  readiness.analysis_status,
  readiness.luna_attempted,
  CAST(NULL AS VARCHAR) AS retrieval_gold_status,
  CAST(NULL AS DOUBLE) AS verifier_reward
FROM projection CROSS JOIN readiness
ORDER BY projection.task_id;
