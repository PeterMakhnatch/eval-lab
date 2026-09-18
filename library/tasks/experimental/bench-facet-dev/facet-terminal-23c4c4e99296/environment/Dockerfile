FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git jq && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /task_file
COPY task_file/ /task_file/
RUN mkdir -p /task_file/output
