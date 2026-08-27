FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

WORKDIR /app
RUN mkdir -p /app/src /app/output

COPY inputs.json /app/src/inputs.json
COPY dag_spec.json /app/src/dag_spec.json
COPY dag_code.py /app/src/dag_code.py
