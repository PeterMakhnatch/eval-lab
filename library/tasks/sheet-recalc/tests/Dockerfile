FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        util-linux \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    pip3 install --break-system-packages --no-cache-dir \
        pytest==8.4.1 \
        pytest-json-ctrf==0.3.5

COPY test_sheet.py runner.py sheet_formula.py test.sh /tests/
RUN chmod 700 /tests && chmod +x /tests/test.sh

WORKDIR /tests
