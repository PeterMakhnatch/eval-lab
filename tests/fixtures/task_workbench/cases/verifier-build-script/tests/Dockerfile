FROM ubuntu@sha256:678c6550cc43645e08669028bc177f50be4e7c5b8cca677067b1914d4afc7a03

# Nothing on any Dockerfile line names a network or a package manager. The fetch
# lives in a copied script, and Harbor builds this image from tests/ with
# extra_docker_compose cleared, so no build.network=none overlay applies.
COPY . /tests
RUN chmod +x /tests/test.sh /tests/verify.sh /tests/bootstrap.sh
RUN sh /tests/bootstrap.sh
