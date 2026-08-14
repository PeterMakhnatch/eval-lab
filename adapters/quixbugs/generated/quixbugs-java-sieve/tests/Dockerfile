FROM gradle:8.10.2-jdk11@sha256:d442dbb806f7d8a27f736e725e82fd3b2bde83703b5a86a4d80c3e1d9e55c72e

USER root
RUN mkdir -p /opt/quixbugs-verifier \
    && curl -fsSL https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar \
        -o /opt/quixbugs-verifier/junit-4.13.2.jar \
    && echo "8e495b634469d64fb8acfa3495a065cbacc8a0fff55ce1e31007be4c16dc57d3  /opt/quixbugs-verifier/junit-4.13.2.jar" | sha256sum -c - \
    && curl -fsSL https://repo1.maven.org/maven2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar \
        -o /opt/quixbugs-verifier/hamcrest-core-1.3.jar \
    && echo "66fdef91e9739348df7a096aa384a5685f4e875584cce89386a7a47251c4d8e9  /opt/quixbugs-verifier/hamcrest-core-1.3.jar" | sha256sum -c -

WORKDIR /tests
COPY . /tests/
COPY support/java_programs/ /app/src/main/java/java_programs/
RUN chmod -R a-w /tests /opt/quixbugs-verifier \
    && mkdir -p /app/src/main/java/java_programs /logs/verifier /tmp/quixbugs-build \
    && chmod 755 /app /app/src /app/src/main /app/src/main/java \
        /app/src/main/java/java_programs /logs /logs/verifier \
    && chown -R gradle:gradle /tmp/quixbugs-build
