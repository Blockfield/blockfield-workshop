#!/bin/bash
# Run the site's admin (astro dev) against the stand's controller output.
# Needs $STAND_DIR/web-runtime-env.json: {"BF_GATE_FILE": "...controller/public.json", "BF_ADMIN_DIR": "...",
# "BF_INBOX_DIR": "...", ...same admin/gate env the site's README documents} plus its own DB/session secrets.
set -eu
STAND_DIR="${STAND_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bf-workshop-stand}"
WORKSPACE="${BF_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)}"
WEB_DIR="${BF_WEB_DIR:-$WORKSPACE/blockfield-web}"
eval "$(python3 -c "import json,shlex;[print(f'export {k}={shlex.quote(v)}') for k,v in json.load(open('$STAND_DIR/web-runtime-env.json')).items()]")"
cd "$WEB_DIR"
NODE_BIN="${NODE:-$(mise which node)}"
export PATH="$(dirname "$NODE_BIN"):$PATH"
exec node_modules/.bin/astro dev --host 127.0.0.1 --port "${BF_WEB_PORT:-4331}"
