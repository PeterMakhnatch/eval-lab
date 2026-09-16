FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    nginx-light ca-certificates sqlite3 python3 less vim-tiny jq && \
    rm -rf /var/lib/apt/lists/*

COPY friction/install_friction.sh /usr/local/sbin/install_friction.sh
RUN chmod +x /usr/local/sbin/install_friction.sh && \
    /usr/local/sbin/install_friction.sh && \
    rm -f /usr/local/sbin/install_friction.sh

COPY fixtures/opt/platform /opt/platform
RUN chmod -R a+rX /opt/platform

WORKDIR /opt/platform
