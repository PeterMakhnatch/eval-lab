FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir pytest==8.3.4

WORKDIR /app
COPY project/ /app/

# Harness scratch: a stale copy of the acceptance file shipped with the image.
COPY leaked-harness/ /opt/task-harness/

RUN mkdir -p /logs/verifier
