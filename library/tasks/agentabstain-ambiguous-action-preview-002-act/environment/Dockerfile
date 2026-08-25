FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

WORKDIR /app

COPY initial_state.json /app/initial_state.json
COPY runtime.py /app/runtime.py
COPY TOOLS.md /app/TOOLS.md
COPY entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /app/state \
    && cp /app/initial_state.json /app/state/state.json \
    && : > /app/state/calls.jsonl \
    && chmod +x /app/entrypoint.sh /app/runtime.py

ENV AGENTABSTAIN_STATE_FILE=/app/state/state.json
ENV AGENTABSTAIN_CALLS_LOG=/app/state/calls.jsonl
ENV AGENTABSTAIN_INITIAL_STATE=/app/initial_state.json

ENTRYPOINT ["/app/entrypoint.sh"]
