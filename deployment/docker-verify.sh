#!/bin/sh
# W04 Docker packaging verification (runs on Boat, never on the owner PC).
# Uses synthetic named volumes only; no production data, app or credential.
# Usage: sh deployment/docker-verify.sh
set -eu

IMAGE=workshop-controller:w04
SECRETS=$(mktemp -d)
cleanup() { rm -rf "$SECRETS"; }
trap cleanup EXIT

echo "== build =="
docker build -t "$IMAGE" .

echo "== non-root user =="
uid=$(docker run --rm --entrypoint python3 "$IMAGE" -I -c 'import os; print(os.getuid())')
echo "uid=$uid"
test "$uid" = "1000"

echo "== pinned toolchain + standard zlib =="
docker run --rm --entrypoint python3 "$IMAGE" -I -c 'import sys, zlib; assert sys.version_info[:3] == (3, 12, 14), sys.version; zlib.compress(b"blockfield"); print(sys.version.split()[0], zlib.ZLIB_VERSION)'

echo "== synthetic volumes + dummy secrets =="
for name in w04state w04data w04locks; do docker volume rm -f "$name" >/dev/null 2>&1 || true; done
docker volume create w04state >/dev/null
docker volume create w04data >/dev/null
docker volume create w04locks >/dev/null
printf 'dummy-ghcr-token' >"$SECRETS/ghcr-package-token"
printf 'https://dokploy.local/api/webhook/deploy/00000000-0000-0000-0000-000000000000' >"$SECRETS/workshop-deploy-webhook-url"
printf 'dummy-rcon-password' >"$SECRETS/workshop-rcon-password"
# Secret pointers: the entrypoint reads credentials only from these files.
set -- -e BF_GHCR_PACKAGE_TOKEN_FILE=/run/secrets/ghcr-package-token \
    -e BF_WORKSHOP_DEPLOY_WEBHOOK_URL_FILE=/run/secrets/workshop-deploy-webhook-url \
    -e BF_WORKSHOP_RCON_PASSWORD_FILE=/run/secrets/workshop-rcon-password

echo "== run entrypoint with Game off =="
docker run -d --name w04ctl \
    -v w04state:/state -v w04data:/data -v w04locks:/locks \
    -v "$SECRETS/ghcr-package-token:/run/secrets/ghcr-package-token:ro" \
    -v "$SECRETS/workshop-deploy-webhook-url:/run/secrets/workshop-deploy-webhook-url:ro" \
    -v "$SECRETS/workshop-rcon-password:/run/secrets/workshop-rcon-password:ro" \
    "$@" -e BF_STEP_INTERVAL_S=2 -e BF_STEP_FOLLOW_S=0 \
    -e BF_MAP_ARCHIVE_DIR=/data/history -e BF_MAP_ACTIVE_DIR=/data/active \
    -e BF_TEMPLATES_DIR=/data/templates -e BF_PUBLISHED_MAPS_DIR=/data/published \
    -e BF_GAME_MAPS_DIR=/data/game-maps \
    "$IMAGE" >/dev/null
sleep 12

echo "== health with Game off =="
docker exec w04ctl python3 -I -c 'import json,urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=10); b=json.loads(r.read()); assert r.status==200 and b["status"]=="ok", b; print(b)'

echo "== non-root state writes =="
docker exec w04ctl sh -c 'touch /state/.w04-probe && rm /state/.w04-probe && echo "state dir writable"'
docker exec w04ctl python3 -I /app/workshopctl.py --backend dokploy \
    --state-dir /state members >/dev/null
test "$(docker exec w04ctl stat -c %u /state/admin/members.json)" = "1000"
docker exec w04ctl python3 -I -c 'import json,pathlib; print("members:", json.loads(pathlib.Path("/state/admin/members.json").read_text()))'

echo "== read-only refusal (not just a label) =="
docker run --rm -v w04data:/ro:ro --entrypoint sh "$IMAGE" -c 'if touch /ro/must-fail 2>/dev/null; then echo "RO BYPASS"; exit 7; else echo "ro mount refused the write"; fi'

echo "== read-only rootfs still healthy =="
docker run -d --name w04ro --read-only --tmpfs /tmp \
    -v w04state:/state -v w04data:/data -v w04locks:/locks \
    -v "$SECRETS/ghcr-package-token:/run/secrets/ghcr-package-token:ro" \
    -v "$SECRETS/workshop-deploy-webhook-url:/run/secrets/workshop-deploy-webhook-url:ro" \
    -v "$SECRETS/workshop-rcon-password:/run/secrets/workshop-rcon-password:ro" \
    "$@" -e BF_STEP_INTERVAL_S=2 -e BF_STEP_FOLLOW_S=0 -e BF_HEALTH_PORT=8081 \
    -e BF_MAP_ARCHIVE_DIR=/data/history -e BF_MAP_ACTIVE_DIR=/data/active \
    -e BF_TEMPLATES_DIR=/data/templates -e BF_PUBLISHED_MAPS_DIR=/data/published \
    -e BF_GAME_MAPS_DIR=/data/game-maps \
    "$IMAGE" >/dev/null
sleep 10
docker exec w04ro python3 -I -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8081/healthz",timeout=10).status==200; print("ro-rootfs health ok")'
docker rm -f w04ro >/dev/null

echo "== restart preserves state, lock held by a sleeper fails the second writer closed =="
before=$(docker exec w04ctl stat -c %Y /state/admin/members.json)
docker stop w04ctl >/dev/null
docker start w04ctl >/dev/null
sleep 8
after=$(docker exec w04ctl stat -c %Y /state/admin/members.json)
echo "mtime $before -> $after"
test "$before" = "$after"
docker exec w04ctl python3 -I -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=10).status==200; print("post-restart health ok")'
# Hold /state/controller.lock so the second writer is deterministically
# contended: it must exit BUSY (75), never silently succeed (0).
docker exec -d w04ctl python3 -I -c 'import fcntl,time; fd=open("/state/controller.lock","w"); fcntl.flock(fd, fcntl.LOCK_EX); time.sleep(60)' >/dev/null
sleep 2
code=0
docker run --rm -v w04state:/state -v w04data:/data -v w04locks:/locks \
    -v "$SECRETS/ghcr-package-token:/run/secrets/ghcr-package-token:ro" \
    -v "$SECRETS/workshop-deploy-webhook-url:/run/secrets/workshop-deploy-webhook-url:ro" \
    -v "$SECRETS/workshop-rcon-password:/run/secrets/workshop-rcon-password:ro" \
    "$@" -e BF_STEP_FOLLOW_S=0 \
    -e BF_MAP_ARCHIVE_DIR=/data/history -e BF_MAP_ACTIVE_DIR=/data/active \
    -e BF_TEMPLATES_DIR=/data/templates -e BF_PUBLISHED_MAPS_DIR=/data/published \
    -e BF_GAME_MAPS_DIR=/data/game-maps \
    --entrypoint python3 "$IMAGE" -I /app/workshopctl.py \
    --backend dokploy --state-dir /state step >/dev/null 2>/tmp/w04-second-writer.err || code=$?
echo "second-writer exit=$code"
test "$code" = "75"

echo "== cleanup =="
docker rm -f w04ctl >/dev/null
docker volume rm -f w04state w04data w04locks >/dev/null
echo "W04 DOCKER VERIFY OK"
