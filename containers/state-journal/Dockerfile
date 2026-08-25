FROM python:3.12.11-alpine3.22@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757

COPY watch.py /usr/local/bin/evallab-state-watch
COPY producer.py /usr/local/bin/producer.py

ENTRYPOINT ["python3", "/usr/local/bin/evallab-state-watch"]
