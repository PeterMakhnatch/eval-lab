FROM python:3.12.11-alpine3.22@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757

WORKDIR /app
COPY pyproject.toml uv.lock /app/
COPY src /app/src
RUN pip install --no-cache-dir uv==0.9.24 \
    && uv sync --locked --no-dev --no-install-project \
    && uv pip install --no-deps .

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONPATH=/app/src
USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/python", "-m", "evallab.ops_continuous"]
CMD ["validate"]
