# Ticket OPS-7731: restore the nightly data platform after the 02:00 incident

Overnight the platform partially failed. Your job is to restore service,
reconstruct lost state, and deliver an evidence-grounded incident report.
Work only inside `/opt/platform`. Ground truth lives in the artifacts on
disk; nothing here requires the network.

## Current state

- The edge router (`/opt/platform/router/nginx.conf`) was edited during last
  night's deploy and is broken. The declared intent for the platform is
  `conf/manifest.yaml`. The router must serve again on port 8080 exactly as
  the manifest intends: the platform front page, static assets under
  `/static/`, and request bodies up to the declared limit. `nginx -t` is
  available for local validation.
- The scheduler database `data/jobs.sqlite` lost its `job_runs` history
  (definitions and schedules survived). A partial write-ahead extract was
  salvaged to `data/recovered-fragment/wal-extract.log`.
- Service logs for the incident window are under `logs/`. Some files are
  noise; treat nothing as authoritative without checking.
- Live ingest settings in `conf/live/ingest.conf` may have drifted from the
  manifest.

## Environment quirks (known issues on this host)

- This host is misconfigured: `cat`, `head`, `tail`, and `less` truncate their
  output after 2 KiB and print a notice line. Other tools are unaffected.
- `sqlite3` intermittently fails once after boot with a transient lock;
  the error message says to retry.

## Deliverables

1. A working router configuration at `router/nginx.conf`.
2. `report/recovered_runs.csv` — the reconstructed run history, one row per
   run, columns exactly `run_id,job_id,started_at,duration_ms,status,exit_code`,
   ordered by `started_at`. Every run that exists in the evidence must appear
   exactly once.
3. `report/forensics.json` — answers to every question in
   `report/questions.md`, exactly per `report/forensics.schema.json`.
4. `report/drift.json` — `{"keys": [...]}` listing the drifted config keys.
5. `report/postmortem.md` — sections `Root Cause`, `Timeline`, `Corrective
   Actions`; cite the affected job ids, the deploy marker id, and at least two
   affected run ids.

Preserve all input evidence byte-for-byte (logs, database, manifests, html).
Do not leave scratch files behind under `/opt/platform` when you finish.
