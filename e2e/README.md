# Workshop E2E stand (blockfield-releases#8, criterion 14)

Integration/E2E harness that drives the **real** `workshopctl`/`controller.py` (and, for the proxy/site
scenarios, the real Velocity+blockfield-proxy+Limbo and the site admin) against native JVMs on this
machine instead of the Docker Swarm services `tools/workshop/README.md` targets in production. One
physical backend at a time, same as prod: `StandBackend` (`stand.py`) mirrors `docker_backend.DockerBackend`
but starts/stops `fabric-server-launch.jar` directly through `agent-process` and reads `.blockfield-ready.json`
from two plain directories instead of two Dokploy apps.

This replaces the throwaway harness an earlier acceptance run built ad hoc under
`/var/mnt/projects/.bf-accept8/`; the evidence from that run (screenshots, journals, bug repros) stays
there, unchanged, as the record of blockfield-releases#8's manual acceptance pass. These scripts are the
portable, repeatable version of the same scenarios.

## Env

| Var | Default | Meaning |
|---|---|---|
| `BF_WORKSPACE` | parent of this repo checkout | directory containing sibling repos (`BlockField-Private-Project`, `blockfield-web`) |
| `STAND_DIR` | `$XDG_CACHE_HOME/bf-workshop-stand` or `~/.cache/bf-workshop-stand` | stand data: `game/`, `workshop/`, `lock/`, `controller/`, `candidates/`, `evidence/` (not `tools/workshop/e2e/` itself, which is code) |
| `JAVA` | `mise which java` (run in `dev/rig`, same Temurin 21 pin) | java binary for the stand JVM |
| `CLAUDE_CODE_SESSION_ID` / `BF_RIG_OWNER` | — (required) | `agent-process` owner; whichever is set first |
| `STAND_XMX` | `1536m` | stand JVM heap; raise if you need production-realistic memory numbers and the host has headroom |

All scripts import `paths.py` for this resolution; nothing else in the directory hardcodes a machine path.

## Bootstrap (once per machine)

The stand needs two independent copies of a packed server, matching prod's "same image, two apps" shape.
Reuse `dev/rig/server.sh pack` to build one, then clone it:

```sh
export BF_RIG_OWNER="$CLAUDE_CODE_SESSION_ID"   # dev/rig doesn't read CLAUDE_CODE_SESSION_ID directly
STAND_DIR="${STAND_DIR:-$HOME/.cache/bf-workshop-stand}"
( cd "$BF_WORKSPACE/BlockField-Private-Project/dev/rig" && S="$STAND_DIR/game" ./server.sh pack )  # or point S at any existing pack
mkdir -p "$STAND_DIR/lock"
cp -a "$STAND_DIR/game" "$STAND_DIR/workshop"       # independent data dirs, same mods/config
rm -rf "$STAND_DIR/workshop/world"                   # workshop starts its own world on first boot
```

`stand.py`'s own `__main__` creates `controller/`, `workshop-admin/`, `workshop-inbox/`, `published/`,
`candidates/`, `map-backups/` and `evidence/` under `STAND_DIR` on first run.

## Scenarios

Each ends with a clean `journal_stand.py check` (0 violations = max 1 stand java at every sample, and
`gate` only `public`/`draining` while backend is `game`, only `builders`/`testers` while backend is `workshop`).
Start the journal once, in a second terminal, for the whole session:

```sh
export STAND_DIR CLAUDE_CODE_SESSION_ID
python3 tools/workshop/e2e/journal_stand.py record "$STAND_DIR/evidence/journal.jsonl"
```

### 1. Admin: create/resume a session, pick maps and members

```sh
python3 tools/workshop/e2e/stand.py members <adminDraslId>:Nether:admin <builderDraslId>:Builder:builder:fields+konstaxa
python3 tools/workshop/e2e/stand.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
bash tools/workshop/e2e/settle.sh workshop
```
Expected: phase settles on `workshop`, gate `builders`, `STAND_DIR/workshop-admin/session.json` has the new
session id and the two maps. Re-running `request workshop --session <same-id>` resumes it (same maps, no new
session). Proves: criterion "administrator creates/resumes a session, picks available maps and members without
hand-editing files."

### 2. One backend, whole switch cycle, with injected faults

```sh
echo '{}' > "$STAND_DIR/faults.json"                         # baseline: no faults
python3 tools/workshop/e2e/stand.py request game
bash tools/workshop/e2e/settle.sh game
python3 -c "import json;json.dump({'start':'once'}, open('$STAND_DIR/faults.json','w'))"   # inject: next start fails
python3 tools/workshop/e2e/stand.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
bash tools/workshop/e2e/settle.sh ERROR
python3 tools/workshop/e2e/stand.py request game --recover
bash tools/workshop/e2e/settle.sh game
python3 tools/workshop/e2e/journal_stand.py check "$STAND_DIR/evidence/journal.jsonl"
```
`faults.json` keys (`stand.py fault()`): `preflight`, `save`, `stop`, `start`, `start_crash` — value `true`
(every attempt) or `"once"` (consumed after firing, logged to `evidence/faults-fired.log`). Expected: the
faulted transition ends in `ERROR` with an `errorMessage`, never a second java process; `--recover` returns to
`game`. Proves: "GAME → WORKSHOP → TEST → WORKSHOP → GAME never runs two full Minecraft backends, confirmed by
an integration test/process journal, including failures."

### 3. Concurrent requests

```sh
python3 tools/workshop/e2e/stand.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')" &
python3 tools/workshop/e2e/stand.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
wait
```
Expected: one accepts, the other exits 1 with "stale state revision" (or exit 75 if it hit the controller lock
mid-step); only one switch proceeds. Proves the same criterion as #2 ("... including concurrent requests"), plus
`tools/workshop/README.md`'s CAS/lock design.

### 4. Crash / SIGKILL restart

```sh
python3 tools/workshop/e2e/stand.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
bash tools/workshop/e2e/settle.sh workshop
PID=$(python3 -c "import json;print(json.load(open('$STAND_DIR/rig-process.json'))['id'])")
kill -9 "$(agent-process status "$PID" --owner "$CLAUDE_CODE_SESSION_ID" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pid"])')"
bash tools/workshop/e2e/settle.sh workshop 40   # controller notices, restarts the same session
```
Expected: the controller brings the same session back up (no data loss, no second java); world/draft files on
disk are unchanged. Proves persistence-under-crash and the "recoverable, closed while uncertain" acceptance line.

### 5. Pause/resume and map removal

```sh
python3 tools/workshop/e2e/stand.py request game            # "pause" building = go back to game without publishing
bash tools/workshop/e2e/settle.sh game
python3 tools/workshop/e2e/stand.py request workshop --session <same-session-id>   # resume: same id, same maps
bash tools/workshop/e2e/settle.sh workshop
python3 tools/workshop/e2e/inbox.py mode profile=workshop session=resume maps='["fields"]'   # drop konstaxa from the session
bash tools/workshop/e2e/settle.sh workshop
```
Expected: draft world files for the removed map stay on disk (only the session's map list shrinks); resuming
with the full list again still finds the draft. Proves "world and settings survive a normal and crash restart,
pause/resume, and removing a map from the session; match cleanup doesn't delete working data."

### 6. Proxy gate (ordinary player / builder / techworks)

Needs the real proxy + a fake Drasl (or the site's) pointed at the stand's `gate.json`/`public.json`:

```sh
python3 tools/workshop/e2e/fake_drasl.py 8085 &
python3 tools/workshop/e2e/ping.py 127.0.0.1 <proxy-port>       # MOTD while backend is game vs workshop vs stopped
python3 tools/workshop/e2e/probe.py Player115 "$STAND_DIR/evidence/probe.jsonl" "chat hi" "sleep 1"
```
Expected: an allowed builder reaches the workshop; an ordinary account gets the maintenance message on connect,
reconnect and backend switch, and is never auto-returned to the workshop; direct connection to the backend port
is refused ("requires you to connect with Velocity"). Proves the proxy-gate criteria.

### 7. ACL / WorldEdit / online revocation

```sh
python3 tools/workshop/e2e/probe.py Player115 "$STAND_DIR/evidence/acl.jsonl" \
  "/op Player115" "/stop" "/gamemode creative" "/give Player115 diamond" "//set stone" "//undo"
python3 tools/workshop/e2e/inbox.py members members='[{"draslId":"<adminId>","name":"Nether","role":"admin"}]'  # drop the builder
bash tools/workshop/e2e/settle.sh workshop
python3 tools/workshop/e2e/probe.py Player115 "$STAND_DIR/evidence/acl.jsonl" "//set stone"   # after revoke
```
Expected: op/console-only commands refused for a builder; `//set`/`//undo`/`//redo` on permitted maps work; a
forged `/api/workshop/submit`/`/api/workshop/access` (site-side, see `tools/workshop/README.md`) without a valid
key/actor/map is rejected; after revocation the account is sent to the lobby on the proxy's next tick and further
edits are refused. Proves the ACL/WorldEdit/revocation criterion.

### 8. Memory measurement

```sh
python3 tools/workshop/e2e/journal_stand.py record "$STAND_DIR/evidence/memory.jsonl"   # rss_mb + heap every ~5s
# drive a switch and a heavy WorldEdit op (scenario 1/7) while it runs, then Ctrl-C
python3 -c "
import json
rows=[json.loads(l) for l in open('$STAND_DIR/evidence/memory.jsonl') if l.strip()]
print('max rss_mb', max((r.get('rss_mb',0) for r in rows), default=0))
print('max heap used_mb', max((r['heap']['used_mb'] for r in rows if r.get('heap')), default=0))
"
```
Expected: exactly one JVM's RSS/heap at a time (no second heap requirement); numbers stay under `STAND_XMX`.
Proves "documented limits and measured container/RSS/heap memory during building, switching and testing."

## Package validation (criterion 14, unit side)

`../test_map_package_attacks.py` (in `tools/workshop/`, runs under
`python3 -m unittest discover -s tools/workshop`) builds its own valid candidate package in a temp dir and
feeds `map_package.validate()` a battery of hostile archives, so this part of criterion 14 runs in the release
job instead of needing a live stand. See that file's docstring for which of the original 19 hostile cases from
the ad-hoc `package_attacks.py` are new coverage vs. already-identical existing tests in `test_map_package.py`.

## Not automated here

Disk-full/ENOSPC (the ad-hoc run could only simulate it without root — see
`/var/mnt/projects/.bf-accept8/evidence/c9-failures`), a real Dokploy redeploy or `systemctl restart docker`
mid-switch, and the launcher's own maintenance-screen rendering are Docker/prod-specific or need root; they stay
manual per `tools/workshop/README.md`'s "Process journal acceptance test" section.
