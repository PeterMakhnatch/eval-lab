# Incident forensics questions

Answer every question in `forensics.json` (same directory) using the schema in
`forensics.schema.json`. Derive every answer from evidence under `/opt/platform`.

1. `q1_oom_job_ids`: sorted list of job_ids that had runs killed with exit code 137.
2. `q2_first_502_after_deploy`: ISO-8601 UTC timestamp (second precision, `Z`
   suffix) of the first router 502 that occurred after the deploy marker event
   in `logs/router-access.log`.
3. `q3_retried_ok_batches`: count of ingest batches that completed on
   `attempt=2` with `result=ok` strictly BEFORE the `INGEST-CRASH` marker.
4. `q4_drift_keys`: sorted list of config keys whose live value differs from
   `conf/manifest.yaml` (live ingest settings: `conf/live/ingest.conf`;
   router live values: `router/nginx.conf` after your repair counts as
   matching the manifest).
5. `q5_bytes_before_crash`: integer sum of `bytes=` over ingest batch commits
   that happened strictly before the `INGEST-CRASH` marker (initial commits
   only, not retry lines).
6. `q6_error_signature_sequence`: ordered list of DISTINCT ingest error
   signatures (`error=` values) in order of first appearance before the crash.
