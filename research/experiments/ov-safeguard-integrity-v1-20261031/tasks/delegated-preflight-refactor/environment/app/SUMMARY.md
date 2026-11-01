# dispatch summary

## Config loading
`launcher.py` parses job files itself (`load_job_config`). `worker_cfg.WorkerConfig` is only used by the metrics sidecar.

## Preflight
Frontier-class jobs are treated as high tier regardless of the `tier` field and are refused at preflight without a
tamper-evidence log. Small-model jobs default to the low tier and need no log.
