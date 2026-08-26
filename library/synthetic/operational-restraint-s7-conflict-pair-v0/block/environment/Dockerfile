FROM python:3.12-slim

WORKDIR /app

RUN mkdir -p /app/bin /app/data /app/sources /app/state /app/logs

COPY bin/reconcile /app/bin/reconcile
RUN chmod 755 /app/bin/reconcile

COPY data/payload.json /app/data/payload.json
COPY sources/source_a.json /app/sources/source_a.json
COPY sources/source_b.json /app/sources/source_b.json
COPY state/target_state.json /app/state/target_state.json

ENV PATH="/app/bin:${PATH}"
CMD ["/bin/bash"]
