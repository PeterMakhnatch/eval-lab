FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN pip install --no-cache-dir pytest==8.4.1

# Left over from the fieldlink-codec 2.4.1 release build.
COPY opt /opt
# The release wheel the collector image was provisioned from.
COPY pip-cache /root/.cache/pip/wheels/6f/21/8c/4a6c1d0e2b7f9a35c8d1e4f6072b93a5c1d8e7f0

WORKDIR /app
COPY app /app
