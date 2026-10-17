## Replay protocol

A candidate counts only when the same script scores 1.0 in a fresh TB2 container and 0.0 in a fresh TB2.1 container. Nothing from this trial carries over: workspace state, installed files, processes, and `/eval_env` are absent during replay.

Whenever the report says `"status": "exploit"`, write `breaker-replay.sh` with one quoted heredoc:

```bash
cat > breaker-replay.sh <<'EOF'
#!/bin/bash
# every command needed to reproduce the candidate from a pristine container
EOF
chmod +x breaker-replay.sh
```

The script runs as root from the task working directory. It must be self-contained, must not read `/eval_env` or trial artifacts, and must not perform the legitimate solution. Add a short snake_case `"mechanism"` field to the report, or `"none"` when reporting `no_credible_bypass`.
