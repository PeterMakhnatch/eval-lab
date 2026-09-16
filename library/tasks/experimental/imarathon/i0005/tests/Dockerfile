FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    nginx-light sqlite3 python3 ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY verify_imarathon.py expected_facts.json test.sh /tests/
RUN chmod +x /tests/test.sh
WORKDIR /tests
