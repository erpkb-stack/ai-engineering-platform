# service-catalog image (Phase 21, ADR-033; the service: Phase 20, ADR-032).
# Build (from the repo root): make images. Tests are NOT run here: they need the compose
# Postgres, and `make catalog-build` runs all 21 of them on the host first.
# Its ignore file is catalog.Dockerfile.dockerignore: only the catalog sources are sent.

ARG MAVEN_IMAGE=maven:3.9-eclipse-temurin-21
ARG JRE_IMAGE=eclipse-temurin:21-jre-jammy

# ---------------------------------------------------------------- build: the same pom as the host
FROM ${MAVEN_IMAGE} AS build
WORKDIR /src
COPY services/service-catalog/pom.xml pom.xml
COPY services/service-catalog/openapi.yaml openapi.yaml
COPY services/service-catalog/src src
# the Maven cache survives between builds (BuildKit cache mount), so only a pom change downloads
RUN --mount=type=cache,target=/root/.m2 mvn -q -B -DskipTests package

# ---------------------------------------------------------------- the OTel Java agent (Phase 23a)
# Pinned version from Maven Central; Maven checks it against Central's checksum. A pinned
# sha256 in this file is [Prod] (the sandbox that wrote this cannot reach Central).
FROM ${MAVEN_IMAGE} AS otel
ARG OTEL_JAVAAGENT_VERSION=2.26.1
# its own cache id: BuildKit runs this stage beside `build`, two Maven writers on one local
# repository can corrupt it (review)
RUN --mount=type=cache,id=m2-otel,target=/root/.m2 \
    mvn -q -B --strict-checksums dependency:get -Dtransitive=false \
      -Dartifact=io.opentelemetry.javaagent:opentelemetry-javaagent:${OTEL_JAVAAGENT_VERSION} \
 && mkdir -p /otel \
 && cp /root/.m2/repository/io/opentelemetry/javaagent/opentelemetry-javaagent/${OTEL_JAVAAGENT_VERSION}/opentelemetry-javaagent-${OTEL_JAVAAGENT_VERSION}.jar \
      /otel/opentelemetry-javaagent.jar

# ---------------------------------------------------------------- runtime: JRE only, non-root
FROM ${JRE_IMAGE} AS runtime
# curl for the HEALTHCHECK, always installed (the image must not depend on what the base has)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 aeoi \
 && useradd --system --uid 10001 --gid aeoi --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin aeoi
WORKDIR /app
COPY --from=build /src/target/service-catalog.jar /app/service-catalog.jar
COPY --from=otel /otel/opentelemetry-javaagent.jar /app/otel/opentelemetry-javaagent.jar
# AEOI_REPO_ROOT: application.yml reads the JWT public key from <root>/secrets/jwt_public.pem
ENV AEOI_REPO_ROOT=/app \
    JAVA_OPTS=-Xmx256m
USER 10001:10001
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --start-interval=2s --retries=3 \
  CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:8010/actuator/health/readiness"]
# the DB password: a mounted FILE (CATALOG_DB_PASSWORD_FILE) read into the java PROCESS env only -
# never baked in, never in the container's env (docker inspect).
# Phase 23a (ADR-035): the OTel agent ONLY when AEOI_OTEL_ENDPOINT is set (like the Python
# services); it captures no headers by default and sanitizes JDBC statements. Its log export is
# OFF: Spring logs would bypass our redaction (they stay on stdout, as before).
ENTRYPOINT ["/bin/sh", "-c", "if [ -n \"$CATALOG_DB_PASSWORD_FILE\" ]; then CATALOG_DB_PASSWORD=\"$(cat \"$CATALOG_DB_PASSWORD_FILE\")\"; export CATALOG_DB_PASSWORD; fi; if [ -n \"$AEOI_OTEL_ENDPOINT\" ]; then JAVA_OPTS=\"$JAVA_OPTS -javaagent:/app/otel/opentelemetry-javaagent.jar\"; export OTEL_EXPORTER_OTLP_ENDPOINT=\"$AEOI_OTEL_ENDPOINT\" OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf OTEL_SERVICE_NAME=service-catalog OTEL_RESOURCE_ATTRIBUTES=service.namespace=aeoi OTEL_METRIC_EXPORT_INTERVAL=15000 OTEL_LOGS_EXPORTER=none; fi; exec java $JAVA_OPTS -jar /app/service-catalog.jar"]
