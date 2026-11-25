FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq tmux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash agent \
    && mkdir -p /home/agent/workspace \
    && chown -R agent:agent /home/agent/workspace

COPY --chown=agent:agent workspace/ /home/agent/workspace/
RUN chown -R agent:agent /home/agent/workspace
