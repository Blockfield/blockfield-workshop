FROM ghcr.io/blockfield/blockfield-server@sha256:e6bbccc6be8e25c7aa06cc9aa7e35b19fbb5ec9fea5ce08c2fbc3b04a91bbe26
ARG REVISION
LABEL org.opencontainers.image.source="https://github.com/Blockfield/blockfield-workshop" \
      org.opencontainers.image.revision="${REVISION}"
ENV BLOCKFIELD_PROFILE=workshop \
    BLOCKFIELD_SESSION_PURPOSE=workshop \
    BLOCKFIELD_STATS_EXPORT_ENABLED=false
COPY controller.py docker_backend.py workshopctl.py journal.py candidates.py map_package.py map_ids.json /opt/blockfield-workshop/
COPY scripts/toolchain.py scripts/quality.py /opt/blockfield-workshop/scripts/
COPY justfile /opt/blockfield-workshop/
