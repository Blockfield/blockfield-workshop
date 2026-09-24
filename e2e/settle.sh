#!/bin/bash
# step until phase is $1 (or ERROR); prints distinct phases
set -u
A="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STAND_DIR="${STAND_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bf-workshop-stand}"
mkdir -p "$STAND_DIR/evidence"
for i in $(seq 1 ${2:-60}); do
  BF_STEP_FOLLOW_S=0 python3 "$A/stand.py" step >/dev/null 2>>"$STAND_DIR/evidence/steps.err"
  p=$(python3 -c "import json;s=json.load(open('$STAND_DIR/controller/state.json'));print(s['phase'],s['gate'],s.get('error') or '',(s.get('errorMessage') or '')[:100])")
  echo "$(date +%T) $p"
  case "$p" in "$1"*|ERROR*) break;; esac
  sleep 3
done | uniq -f1
