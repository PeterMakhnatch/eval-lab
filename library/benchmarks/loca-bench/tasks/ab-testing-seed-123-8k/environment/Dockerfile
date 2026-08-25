FROM python:3.12-slim
WORKDIR /adapter
COPY . /adapter
ENV PYTHONPATH=/adapter/vendor/loca_upstream/mcp_convert
ENTRYPOINT ["/adapter/entrypoint.sh"]
