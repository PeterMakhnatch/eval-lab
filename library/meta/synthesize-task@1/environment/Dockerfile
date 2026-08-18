FROM python:3.13-slim-bookworm

WORKDIR /app

COPY spec.json /app/spec.json
COPY skeleton /app/skeleton
COPY exemplar /app/exemplar
COPY templates /app/templates

RUN mkdir -p /app/output/task
