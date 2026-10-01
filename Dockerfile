FROM ghcr.io/blockfield/blockfield-server@sha256:c89080e32441deb66ae6591981675f023c0a6520539959db2b10c0eab5e20232
ARG REVISION
LABEL org.opencontainers.image.source="https://github.com/Blockfield/blockfield-workshop" \
      org.opencontainers.image.revision="${REVISION}"
ENV BLOCKFIELD_PROFILE=workshop \
    BLOCKFIELD_SESSION_PURPOSE=workshop \
    BLOCKFIELD_STATS_EXPORT_ENABLED=false
COPY controller.py docker_backend.py workshopctl.py journal.py candidates.py map_package.py limbo.py map_ids.json /opt/blockfield-workshop/
COPY scripts/toolchain.py scripts/quality.py /opt/blockfield-workshop/scripts/
COPY justfile /opt/blockfield-workshop/
