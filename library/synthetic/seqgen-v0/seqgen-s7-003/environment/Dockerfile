FROM python:3.13-slim-bookworm@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

WORKDIR /app

COPY orders.jsonl /app/data/orders.jsonl
COPY rp /app/bin/rp

RUN chmod +x /app/bin/rp && mkdir -p /app/output
