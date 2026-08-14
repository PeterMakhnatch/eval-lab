FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && useradd --create-home --uid 10001 candidate

WORKDIR /tests
COPY . /tests/
RUN chmod -R a-w /tests \
    && mkdir -p /app/python_programs /logs/verifier \
    && chmod 755 /app /app/python_programs /logs /logs/verifier
