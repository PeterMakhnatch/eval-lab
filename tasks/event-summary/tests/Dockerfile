FROM python:3.13-slim-bookworm

COPY . /tests

RUN mkdir -p /app/input /app/output /logs/verifier \
    && chmod +x /tests/test.sh

WORKDIR /app

