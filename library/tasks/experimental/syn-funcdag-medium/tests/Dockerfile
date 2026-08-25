FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p /logs/verifier
COPY golden.json /tests/golden.json
COPY verify.py /tests/verify.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh /tests/verify.py
