FROM python:3.12.11-alpine3.22@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757

WORKDIR /app
COPY scripts/ops/continuous-operator /usr/local/bin/continuous-operator
COPY src/evallab/ops_continuous.py /app/src/evallab/ops_continuous.py

ENV PYTHONPATH=/app/src
ENTRYPOINT ["python3", "-m", "evallab.ops_continuous", "validate"]
