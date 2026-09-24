#!/bin/bash
# Click "Принять <map>" on an older pending candidate, let the controller answer, screenshot the row message.
#   stale_accept.sh <out.png> "<map button label suffix>" [admin base url]
set -eu
STAND_DIR="${STAND_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bf-workshop-stand}"
A="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SESSION="${BF_PW_SESSION:-bf8admin}"
URL="${3:-http://127.0.0.1:${BF_WEB_PORT:-4331}}/admin#workshop"
mkdir -p "$STAND_DIR/pw"
cd "$STAND_DIR/pw"
P(){ playwright-cli -s="$SESSION" "$@"; }
P resize 1440 1000 >/dev/null
P goto "$URL" >/dev/null; sleep 3
P snapshot --filename=sa.yml >/dev/null
B=$(grep -oE "button \"Принять $2\" \[ref=[a-z0-9]+\]" sa.yml | tail -1 | grep -oE 'ref=[a-z0-9]+' | cut -d= -f2)
P click "$B" >/dev/null; sleep 1
P click "getByRole('dialog').getByRole('button', { name: 'Принять', exact: true })" >/dev/null; sleep 1
(cd "$A" && ./settle.sh workshop 2 >/dev/null)
sleep 4
P snapshot --filename=sa2.yml >/dev/null
grep -E 'status \[ref=[a-z0-9]+\]: ' sa2.yml | sed 's/^\s*//' | cut -c1-200
P eval "document.querySelector('#workshop').scrollIntoView()" >/dev/null
P screenshot --filename="$1" >/dev/null
magick "$1" -resize 1568x1568\> -quality 80 "${1%.png}.jpg" && rm "$1"
