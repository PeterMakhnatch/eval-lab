FROM gradle:8.10.2-jdk11@sha256:d442dbb806f7d8a27f736e725e82fd3b2bde83703b5a86a4d80c3e1d9e55c72e

USER root
WORKDIR /app
COPY build.gradle settings.gradle /app/
COPY src/ /app/src/
RUN gradle --no-daemon classes
