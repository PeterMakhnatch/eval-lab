FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p /app/src /app/output

COPY inputs.json /app/src/inputs.json
COPY dag_spec.json /app/src/dag_spec.json
COPY dag_code.py /app/src/dag_code.py
