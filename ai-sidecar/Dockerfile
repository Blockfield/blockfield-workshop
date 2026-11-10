# Workshop AI sidecar image (server#321 U-W1): the game JVM never sees provider keys.
#
# Pinned tags and digests refresh together; re-resolve with the registry API:
#   Accept: application/vnd.oci.image.index.v1+json on
#   https://registry-1.docker.io/v2/library/<name>/manifests/<tag>
#   and read the docker-content-digest response header.
FROM gradle:8.14.2-jdk21-noble@sha256:cf0caeaac06e2824fac26daa6a55728fcb0e0e0e2ff28a430d2292547fe84dff AS build

WORKDIR /src
COPY settings.gradle build.gradle ./
COPY gradle ./gradle
COPY gradlew ./
COPY src ./src
RUN ./gradlew --no-daemon installDist

FROM eclipse-temurin:21-jre-noble@sha256:0eda5830ed7b669abc246a8dc520ea8a23cab06f9a8d4b6964109e2f2394e46f

RUN groupadd -r -g 1000 workshop && useradd -r -u 1000 -g workshop workshop

COPY --from=build /src/build/install/ai-sidecar /app
RUN chmod 0755 /app/bin/ai-sidecar

USER 1000:1000

# Loopback only; the orchestrator probes GET /health (no auth) from outside.
# No in-image curl/python: the JRE image ships neither.
ENV AI_SIDECAR_BIND=0.0.0.0 \
    AI_SIDECAR_PORT=8101 \
    AI_SIDECAR_KEY_FILE=/keys/ai-sidecar.key

EXPOSE 8101

ENTRYPOINT ["/app/bin/ai-sidecar"]
