# Workshop controller image (staged packaging; the host Docker adapter stays
# authoritative, see docs/OPERATIONS.md). No shell tools beyond the stdlib:
# lifecycle goes through in-container RCON/HTTP, never a socket or proxy.
#
# Pinned base: python 3.12.14-slim-bookworm. Refresh tag and digest together
# with: docker buildx imagetools inspect python:3.12.14-slim-bookworm
# Pinned tini: apt resolves it live at build time, so the base digest alone
# does not pin it. Refresh the tini version with the base (#43).
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BF_STATE_DIR=/state \
    BF_GATE_FILE=/state/gate/gate.json \
    BF_PUBLIC_FILE=/state/public/public.json \
    BF_ADMIN_DIR=/state/admin \
    BF_INBOX_DIR=/state/inbox \
    BF_WORKSHOP_DATA=/data \
    BF_LOCK_DIR=/locks \
    BF_HEALTH_PORT=8080

WORKDIR /app

# Pinned toolchain: exact interpreter plus working standard zlib, asserted at
# build time (the host install in docs/OPERATIONS.md runs the same check).
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends tini=0.19.0-1+b3 \
    && rm -rf /var/lib/apt/lists \
    && groupadd -r -g 1000 workshop && useradd -r -u 1000 -g workshop workshop \
    && mkdir -p /app /state/gate /state/public /state/admin /state/inbox /data /locks \
    && chown -R 1000:1000 /state /data /locks \
    && python3 -I -c 'import sys, zlib; assert sys.version_info[:3] == (3, 12, 14), sys.version; zlib.compress(b"blockfield"); print(sys.version.split()[0], zlib.ZLIB_VERSION)'

COPY controller.py releases.py journal.py candidates.py desired.py cp_client.py \
    map_package.py limbo.py map_ids.json docker_backend.py dokploy_backend.py \
    workshopctl.py entrypoint.py /app/
RUN chmod 0644 /app/*.py /app/*.json && chmod 0755 /app/entrypoint.py

USER 1000:1000

# Loopback only: Docker HEALTHCHECK runs inside the container; Dokploy
# publishes no controller port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -I -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

ENTRYPOINT ["tini", "--", "python3", "-I", "/app/entrypoint.py"]
