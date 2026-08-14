FROM python:3.12-slim-bookworm

WORKDIR /app

# sqlite3 gives the evaluated agent a simple way to inspect and edit the local
# database. Python's sqlite3 module is already included in this base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# This script is task-author setup: it creates the initial database before an
# agent starts. The agent receives the resulting database, not this script.
COPY seed_db.py /tmp/seed_db.py
RUN python /tmp/seed_db.py
