FROM python:3.12-slim
RUN pip install --no-cache-dir fastmcp==2.12.4 pyyaml==6.0.2
WORKDIR /adapter
COPY . /adapter
ENV PYTHONPATH=/adapter/vendor/loca_upstream/mcp_convert
ENTRYPOINT ["/adapter/entrypoint.sh"]
