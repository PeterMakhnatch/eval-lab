FROM python:3.13-slim-bookworm@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

COPY . /tests

RUN mkdir -p /app/data /app/output /logs/verifier \
    && chmod +x /tests/test.sh

WORKDIR /app
