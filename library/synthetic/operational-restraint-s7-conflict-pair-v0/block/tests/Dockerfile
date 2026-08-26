FROM python:3.12-slim

WORKDIR /tests

RUN mkdir -p /logs/verifier /tests/fixtures

COPY verify.py /tests/verify.py
RUN chmod +x /tests/verify.py

COPY fixtures/ /tests/fixtures/
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh

CMD ["/tests/test.sh"]
