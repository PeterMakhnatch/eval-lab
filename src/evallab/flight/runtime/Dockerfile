FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bpftrace \
        tcpdump \
        python3 \
        iproute2 \
        iptables \
        traceroute \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/probes.bt /opt/flight/probes.bt
COPY runtime/recorder.py /opt/flight/recorder.py
COPY gateway.py /opt/flight/evallab/flight/gateway.py

ENTRYPOINT ["python3", "/opt/flight/recorder.py"]
