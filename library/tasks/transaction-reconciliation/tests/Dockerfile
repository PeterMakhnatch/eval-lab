FROM python:3.12-slim-bookworm

COPY . /tests

RUN mkdir -p /app/data /logs/verifier \
    && chmod +x /tests/test.sh

WORKDIR /app
