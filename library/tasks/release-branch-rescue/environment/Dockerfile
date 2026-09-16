FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# Persist Git identity for all interactive shells and scripts
ENV GIT_AUTHOR_NAME="Release Bot" \
    GIT_AUTHOR_EMAIL="release@example.com" \
    GIT_COMMITTER_NAME="Release Bot" \
    GIT_COMMITTER_EMAIL="release@example.com"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        less \
        vim-tiny \
        jq && \
    rm -rf /var/lib/apt/lists/*

COPY build_fixture.sh /usr/local/bin/build_fixture.sh
RUN chmod +x /usr/local/bin/build_fixture.sh && \
    /usr/local/bin/build_fixture.sh && \
    rm -f /usr/local/bin/build_fixture.sh

WORKDIR /workspace/release
