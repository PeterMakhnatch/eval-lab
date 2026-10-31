FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir pytest==8.3.4

WORKDIR /app

COPY app/ /app/

RUN mkdir -p /app/reports
