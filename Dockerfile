FROM ghcr.io/blockfield/blockfield-server@sha256:8883b808248fd9c9251cc7131a4db612c22df82f040656f068dfcc33806b2a79
ARG REVISION
LABEL org.opencontainers.image.source="https://github.com/Blockfield/blockfield-workshop" \
      org.opencontainers.image.revision="${REVISION}"
ENV BLOCKFIELD_PROFILE=workshop \
    BLOCKFIELD_SESSION_PURPOSE=workshop \
    BLOCKFIELD_STATS_EXPORT_ENABLED=false
COPY controller.py docker_backend.py workshopctl.py journal.py candidates.py map_package.py limbo.py map_ids.json /opt/blockfield-workshop/
COPY scripts/toolchain.py scripts/quality.py /opt/blockfield-workshop/scripts/
COPY justfile /opt/blockfield-workshop/
