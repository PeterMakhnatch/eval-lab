FROM ubuntu:24.04

# Verifier-only image for separate-mode verification. Built from tests/ as the
# build context. Contains trusted nginx, Python, and test dependencies — never
# the agent's config, backend, or static files. Those arrive as collected
# artifacts, re-materialized at their original absolute paths. If an artifact
# is missing, grading fails; nothing is silently replaced with pristine copies.

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nginx \
        python3 \
        python3-pip \
        ca-certificates \
        procps \
        psmisc \
        curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip3 install --break-system-packages --no-cache-dir \
        pytest==8.4.1 \
        pytest-json-ctrf==0.3.5

COPY backend.py test_proxy.py test.sh /tests/

WORKDIR /app
