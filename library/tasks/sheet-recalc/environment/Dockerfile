FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN pip install --no-cache-dir pytest==8.4.1

WORKDIR /app
COPY sheet /app/sheet
COPY tests /app/tests
