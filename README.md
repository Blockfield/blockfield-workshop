# Workshop: GAME <-> WORKSHOP switch with one Minecraft backend

Refs netherg-io/blockfield-releases#8 (stage a); draining follows #6. Python 3.11+ on Linux, stdlib only.

```sh
python3 -m unittest discover -s tools/workshop -p 'test_*.py' -v   # controller, Docker adapter, CLI, journal, packages
bash distribution/test-backend-lock.sh                              # start-container.sh flock/profile guard
(cd proxy/plugin && ./gradlew test)                                 # proxy gate parsing/routing
python3 tools/workshop/map_package.py candidate.zip --sha256 <pinned-archive-sha256>
```

None of these are live Docker/Minecraft acceptance tests; the process journal below is.

## Architecture (owner decision 2026-09-14)

- Two Dokploy apps on the **same server image**: `blockfield-server` (profile `game`) and `blockfield-workshop`
  (profile `workshop`, 0 replicas outside the workshop, own data, no stats keys, templates read-only).
- Both mount `/srv/minecraft/backend-lock` as `/backend-lock`. `distribution/start-container.sh` takes
  `flock` on `/backend-lock/backend.lock` and starts java only while holding it **and** only if
  `/backend-lock/profile.json` selects its `BLOCKFIELD_PROFILE`. Java inherits the lock fd. A Swarm restart,
  start-first rollback, redeploy or host boot can therefore start at most one JVM, and never the unselected profile.
  Without the mount, `game` starts as before with a loud warning (current prod until reconfigured) and `workshop` refuses.
- Host controller (`controller.py` + `docker_backend.py`) runs from a systemd timer every 15 s (`workshopctl step`);
  the owner issues requests with `workshopctl`. The web admin comes later.
- The proxy reads `gate.json`, shows the maintenance message (join, lobby, server list MOTD) and admits only allowed
  accounts. The site/launcher read `public.json`.
- WorldEdit is not part of stage (a); the workshop profile has only op building.

### Contracts

| File (host path) | Writer | Reader | Content |
| --- | --- | --- | --- |
| `/srv/minecraft/backend-lock/profile.json` | controller `start()` | `start-container.sh`, mod | `{schema:1, profile, generation, session, release}` |
| `<data>/.blockfield-ready.json` | mod on SERVER_STARTED (deleted by container start, SERVER_STARTING/STOPPING and controller before start) | controller | `{profile, generation, session, release}` |
| `/srv/minecraft/workshop/gate/gate.json` | controller after every state write | proxy (`BLOCKFIELD_GATE_FILE`) | `{schema:1, revision, gate: public/draining/closed/builders, backend: game/workshop, message, allowed:[{draslId,name,role,maps?}]}` |
| `/srv/minecraft/workshop/public/public.json` | controller | site (`BLOCKFIELD_GATE_FILE`) | `{mode: game/draining/workshop/maintenance, message, since}` |
| `/srv/minecraft/workshop/controller/` (0700) | controller | — | `state.json`, `members.json`, `message.json`, `controller.lock`, `inbox.lock` |
| `/srv/minecraft/workshop/admin/` (0755, files 0644, `BF_ADMIN_DIR`) | controller | site admin (read-only mount) | `members.json` (current ACL), `status.json` `{schema:1, revision, phase, gate, error, errorMessage, since, deadline, desired, message, updatedAt}`, `results/<requestId>.json` `{id, ok, error, revision, doneAt}` (newest 200 kept) |
| `/srv/minecraft/workshop/inbox/` (root:1000 0770, `BF_INBOX_DIR`) | site admin (uid 1000, `.<id>.tmp` + rename) | controller | `<id>.json` `{schema:1, id, kind: mode/members/message, actor:{draslId,name}, createdAt, ...}` |

`release` is `build` from the image's `/opt/blockfield-build.json` (the modpack tag, e.g. `v2.27.0`). Readiness counts only
when all four fields equal `profile.json` and exactly one java of that app runs. Before the first controller start
(`profile.json` absent) a game readiness with `generation: null` is adopted, so initialising does not restart the game.

Mount the `gate/` and `public/` **directories** read-only, not the files: the controller replaces files atomically
and a single-file bind mount would keep showing the old inode.

## Controller behaviour

`request()` durably stores intent with CAS revisions; `step()` reconciles once under a non-blocking lock.
- Stable phases and DRAINING are written only when phase/gate/error change: no gate flapping, exports keep their mtime.
- `game -> workshop`: gate `draining` (existing game members may still connect), `/bf maintenance drain`, wait for
  `drained=true`, `save-all flush`, scale game to 0 and wait until no task and no java, write `profile.json`, delete
  readiness, scale workshop to 1, open `builders` only on matching readiness.
- `workshop -> game`: `save-all flush`, stop, start game, `public` only on matching game readiness.
- **DRAINING and STARTING have a deadline** (2026-09-18 incident: DRAINING sat for ~2h on a stuck stats-health flag,
  then a mid-operation external restart turned an unresolved STARTING into ERROR with no message). Past the deadline,
  `step()` rolls back to the profile recorded when the switch was requested (`state["rollback"]`) instead of waiting
  or requiring root: DRAINING never touched the old backend, so rollback just restores "desired"; STARTING already
  stopped it, so rollback stops the half-started profile and starts the old one again. `errorMessage`/`since`/`deadline`
  in `status.json` explain why. `BF_DRAIN_TIMEOUT_S` (default 600s, or `BF_DRAIN_TIMEOUT_RANKED_S`/3600s while a
  Ranked campaign is still reported), `BF_DRAIN_HEALTH_GRACE_S` (default 60s: rooms=ranked=pending=0 but stats
  unhealthy rolls back fast rather than waiting on a flag that may never clear) and `BF_START_TIMEOUT_S`
  (default 600s: a cold pack start takes ~2.5 min on homeserver, and a missed deadline costs a second full
  restart of the game) tune this; sizing "the longest match" beyond the ranked default is a manual env override, not
  auto-detected from match length.
- A backend of the *wrong* profile appearing while STARTING is unresolved (an external restart, as above) is fenced
  and retried instead of raising; unrecoverable failures still surface as `ERROR` with `error`+`errorMessage`.
  Any error -> `ERROR`, gate `closed`. The timer never leaves ERROR. `workshopctl request game --recover` fences the
  unselected app (scale 0, wait), then saves/stops whatever runs and starts game.
- `workshopctl cancel` (or inbox kind `cancel`, no root) rolls back a stuck DRAINING/STARTING switch immediately
  instead of waiting for its deadline -- the unprivileged counterpart to `--recover`, which stays root-only and
  ERROR-only.
- STARTING without a visible process waits closed (up to its deadline) and never re-issues start (see REVIEW-2026-09-13.md).
- Missing/invalid `members.json` admits nobody. Any java outside the two apps (pattern `fabric-server-mc.*-launcher.*.jar`)
  is an error: do not run other Fabric servers on this host while the controller is enabled.

`docker_backend.py` executes only: `docker service scale --detach <app>=0|1`, `docker service ps <app> --filter desired-state=running`,
`docker ps --no-trunc --filter label=com.docker.swarm.service.name=<app>`, `docker exec <container> rcon-cli`
with `bf maintenance drain`, `bf maintenance status`, `save-all flush`, and `pgrep -f <java pattern>`; java is mapped
to its container through `/proc/<pid>/cgroup`.

## Site admin inbox

`workshopctl step` (the timer) first applies up to 20 inbox requests, oldest `createdAt` first, then reconciles.
Every request gets exactly one `admin/results/<id>.json` and its inbox file is removed; an existing result only removes
the inbox file (idempotent). Rejected with a result: names not `^[0-9a-f-]{36}\.json$` are ignored, symlinks/non-regular
files, > 65536 bytes, invalid JSON, `schema != 1`, `id` != file name, invalid actor, `createdAt` older than 10 min,
unknown kind. A busy `controller.lock` leaves the request for the next step. Each request is logged with the actor name.

- `mode` `{profile: game|workshop, expectedRevision, recover?}`: same as `workshopctl request` with the current release;
  workshop gets a fresh session UUID; `recover` only toward game and runs the recovery step immediately.
- `members` `{members:[{draslId,name,role,maps?}]}`: full ACL replacement (the site is the source of truth; `workshopctl members`
  is the owner fallback); a list without an `admin` is refused. `maps` is an optional array of map ids (`controller.MAP_IDS`, kept equal to `server/lobby/blockfield/maps/*.json` by
  `test_map_ids_match_lobby_maps`); plus `hub` for building in the workshop hub; missing means all maps and the hub, unknown ids are rejected, and `lead`/`admin` always have all maps regardless of `maps`.
- `message` `{message}`: at most 200 characters, no control characters (Cc) or `§` (same rule as the site), stored in `controller/message.json` and exported to
  gate/public/status; an empty message returns to `BF_MAINTENANCE_MESSAGE`.

## Owner setup (not done by this PR)

Dependencies: server image with blockfield-mod#30 (readiness + workshop profile, stacked on #28 DRAINING), proxy image
from this PR, site with blockfield-web#5, launcher with Blockfield-Launcher#4.

1. Host (root):
   ```sh
   install -d -m 0755 /srv/minecraft/backend-lock /srv/minecraft/workshop /srv/minecraft/workshop/gate /srv/minecraft/workshop/public
   install -d -m 0700 /srv/minecraft/workshop/controller
   install -d -m 0755 /srv/minecraft/workshop/admin /srv/minecraft/workshop/admin/results
   install -d -m 0770 -o root -g 1000 /srv/minecraft/workshop/inbox   # site container uid/gid 1000 writes requests
   install -d -o 1000 -g 1000 /srv/minecraft/workshop/data
   install -d /opt/blockfield-workshop /etc/blockfield
   install -m 0644 tools/workshop/{controller,docker_backend,workshopctl,journal}.py /opt/blockfield-workshop/
   install -m 0600 tools/workshop/systemd/workshop.env.example /etc/blockfield/workshop.env   # edit service names
   install -m 0644 tools/workshop/systemd/blockfield-workshop.{service,timer} /etc/systemd/system/
   systemctl daemon-reload            # do NOT enable the timer yet
   ```
2. Dokploy `blockfield-server` (game): add bind mount `/srv/minecraft/backend-lock` -> `/backend-lock`; env
   `BLOCKFIELD_PROFILE=game`; set **rollback order stop-first** (currently start-first; update order is already stop-first).
   Keep the existing `/srv/minecraft/lobby` and templates mounts (make templates `:ro`).
3. Dokploy: create `blockfield-workshop` from the same image `ghcr.io/netherg-io/blockfield-server:<tag>`, **replicas 0**,
   restart policy any, update and rollback order stop-first, same network as the proxy. Mounts:
   `/srv/minecraft/workshop/data` -> `/data`, `/srv/minecraft/templates/maps` -> `/data/maps` read-only,
   `/srv/minecraft/backend-lock` -> `/backend-lock`. Env: `BLOCKFIELD_PROFILE=workshop`,
   `BLOCKFIELD_SESSION_PURPOSE=workshop`, `BLOCKFIELD_STATS_EXPORT_ENABLED=false`, `EULA`, `TYPE`/`VERSION`/memory
   as the game app, own `RCON_PASSWORD`, same `FABRIC_PROXY_SECRET`; **no** `BLOCKFIELD_STATS_INGEST_*` variables.
   Health check as the game app (`mc-health`, interval 30s, **start period 600s**, 6 retries): the itzg image default
   (start period 120s, 2 retries) kills the workshop as unhealthy mid-startup (2026-09-23 prod smoke test).
   Put the resulting service name into `/etc/blockfield/workshop.env` and into `proxy/velocity.toml` (`workshop = ...`).
4. Proxy app: env `BLOCKFIELD_GATE_FILE=/workshop-gate/gate.json`, mount `/srv/minecraft/workshop/gate` -> `/workshop-gate` read-only.
   Site app: env `BLOCKFIELD_GATE_FILE=/workshop-public/public.json`, mount `/srv/minecraft/workshop/public` read-only;
   for the admin also mount `/srv/minecraft/workshop/admin` read-only and `/srv/minecraft/workshop/inbox` read-write.
5. Initialise before deploying the proxy/site with the env (with the env set and no file both fail closed):
   ```sh
   cd /opt/blockfield-workshop && set -a && . /etc/blockfield/workshop.env && set +a
   python3 workshopctl.py request game --release v2.27.0   # = build in /opt/blockfield-build.json of the running game
   python3 workshopctl.py step                              # expect phase game, gate public, no restart
   systemctl enable --now blockfield-workshop.timer
   ```
6. **Announced proxy restart window** (all players are disconnected once): announce in Discord/hub, wait for no running
   Ranked campaign, deploy the proxy (`Deploy` with `proxy: true`), check `gate.json` is read (proxy log `Gate ...`) and
   that a normal login still reaches the game. Roll back by removing `BLOCKFIELD_GATE_FILE` from the proxy env.

## Operations

```sh
python3 workshopctl.py members e10438e4797b49718cfecec6a8fa171b:Nether:admin <draslId>:<Name>[:builder|lead|admin[:mapId+mapId]]
python3 workshopctl.py members e10438e4797b49718cfecec6a8fa171b:Nether:admin <draslId>:Builder:builder:konstaxa+fields  # restrict to two maps
python3 workshopctl.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
python3 workshopctl.py status          # the timer does the steps; phase DRAINING can last a whole campaign
python3 workshopctl.py request game    # save, stop workshop, start game, open after readiness
python3 workshopctl.py request game --recover   # only from ERROR; read `error` in status first
python3 workshopctl.py cancel                    # abort a stuck DRAINING/STARTING switch now, no root needed
```

Drasl profile ids: `curl https://skins.blockfield.pro/account/users/profiles/minecraft/<Name>`. `members` applies online:
removed accounts are sent to the lobby on the proxy's next tick (3 s). Exit codes: 0 ok, 1 invalid request, 2 ERROR, 75 busy.

## Process journal acceptance test (owner maintenance window)

Run on the host as root, with the env file loaded, in a second terminal for the whole window:

```sh
python3 journal.py record --out /root/workshop-journal-$(date +%F).jsonl   # java per app, lock holder, tasks, gate; 200 ms
python3 journal.py check /root/workshop-journal-*.jsonl                  # exit 1 on any violation
```

Assertions: max java <= 1 at every sample; gate `public`/`draining` only with backend game and no workshop java;
gate `builders` only with backend workshop and no game java. Docker listing is refreshed once per second; java, lock
and gate every sample. Scenarios, each ending in a clean `check`:

1. Switch round-trip: `request workshop`, wait `builders`, builder joins, ordinary account sees the message and stays in the
   lobby; `request game`, wait `public`, ordinary account is returned.
2. Concurrent requests: two `request workshop` at once (one gets 75 or stale revision), `step` while the timer runs.
3. `kill -9` of the workshop java during STARTING (by PID from `status`/journal): stays closed, no second java;
   recover with `request game --recover`.
4. Dokploy redeploy of `blockfield-server` during WORKSHOP: the game task waits in start-container.sh, no java; gate stays `builders`.
5. `systemctl restart docker` during WORKSHOP and during GAME: at most one java after restart, gate follows readiness.
6. Proxy restart during WORKSHOP: after start ordinary accounts see the message, builders enter the workshop.

Record `workshopctl status`, `docker stats --no-stream` (container RSS) and `/bf version` for each scenario in the issue.

## Candidate layout v1

A ZIP contains manifest.json, map.json and world/{region,entities,poi}/r.X.Z.mca. manifest.json has schemaVersion=1, mapId, version, baseVersion and files mapping every non-manifest regular file to its SHA256. map.json.id must match mapId. External archive SHA256 is mandatory; it binds the manifest too. At least one world/region file is required.

The read-only validator copies bounded compressed bytes to private temporary scratch, then checks that exact pinned snapshot, path canonicality, duplicates, special/symlink/encrypted files, permitted layout, expanded/file-count/metadata limits, every member digest, finite/unambiguous JSON and world/config identity. It never extracts into a world, publishes, deletes, or changes a release pointer. Scratch is deleted on close.

Unknown mod sidecars are rejected, NOT silently omitted. Full export compatibility is not established by region/entities/poi alone. Extend the allowlist/schema after testing real mod-dependent data. There is intentionally no archive builder that would pretend a copy of actively changing region files is a consistent backup.

## Still open after stage (a)

Sessions/maps/roles storage, WorldEdit profile and boundaries, web admin/in-game UI, snapshots, candidates/review, TEST
profile (the adapter rejects `test`), publisher, activation/rollback, stats #3 purpose enforcement at the source.
