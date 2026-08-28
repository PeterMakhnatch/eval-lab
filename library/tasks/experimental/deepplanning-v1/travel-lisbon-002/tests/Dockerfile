FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

WORKDIR /app
RUN mkdir -p /logs/verifier /tests
COPY golden.json /tests/golden.json
COPY verify.py /tests/verify.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh /tests/verify.py
