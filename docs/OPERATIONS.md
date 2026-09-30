# Workshop: GAME <-> WORKSHOP switch with one Minecraft backend

Production controller: Linux, Python 3.12.14 / zlib 1.3.2, stdlib only.
Historical design: netherg-io/blockfield-releases#8 (stage a), draining #6.

```sh
just test   # controller, Docker adapter, CLI, journal, packages
just python map_package.py candidate.zip --sha256 <pinned-archive-sha256>
```

None of these are live Docker/Minecraft acceptance tests; the process journal below is.

## Architecture (owner decision 2026-09-14)

- Two Dokploy apps on compatible images: `blockfield-server` (profile `game`) and `blockfield-workshop`
  (its independent image wraps a pinned published server digest; profile `workshop`, 0 replicas outside the workshop, own data, no stats keys, templates read-only).
- Both mount `/srv/minecraft/backend-lock` as `/backend-lock`. `distribution/start-container.sh` takes
  `flock` on `/backend-lock/backend.lock` and starts java only while holding it **and** only if
  `/backend-lock/profile.json` selects its `BLOCKFIELD_PROFILE`. Java inherits the lock fd. A Swarm restart,
  start-first rollback, redeploy or host boot can therefore start at most one JVM, and never the unselected profile.
  Without the mount, `game` starts as before with a loud warning (current prod until reconfigured) and `workshop` refuses.
- Host controller (`controller.py` + `docker_backend.py`) runs from a systemd timer every 15 s (`workshopctl step`), and at once
  when the site drops a request into the inbox (`blockfield-workshop.path`); a step during a switch re-checks every 2 s for up
  to 12 s (`BF_STEP_POLL_S`, `BF_STEP_FOLLOW_S`), so drained/stopped/ready is acted on without waiting for the next tick;
  the owner issues requests with `workshopctl`. The web admin comes later.
- The proxy reads `gate.json`, shows the maintenance message (join, lobby, server list MOTD) and admits only allowed
  accounts. The site/launcher read `public.json`.
- WorldEdit is not part of stage (a); the workshop profile has only op building.

### Contracts

| File (host path)                                                    | Writer                                                                                                                                                                 | Reader                                            | Content                                                                                                                                                                                                                                                     |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/srv/minecraft/backend-lock/profile.json`                          | controller `start()`                                                                                                                                                   | `start-container.sh`, mod                         | `{schema:1, profile, generation, session, release, candidate?}` (profile `test` runs in the workshop app)                                                                                                                                                   |
| `<data>/.blockfield-ready.json`                                     | mod on SERVER_STARTED (deleted by container start, SERVER_STARTING/STOPPING and controller before start)                                                               | controller                                        | `{profile, generation, session, release}`                                                                                                                                                                                                                   |
| `/srv/minecraft/workshop/gate/gate.json`                            | controller after every state write                                                                                                                                     | proxy (`BLOCKFIELD_GATE_FILE`)                    | `{schema:1, revision, gate: public/draining/closed/builders/testers, backend: game/workshop, message, allowed:[{draslId,name,role,maps?}]}`                                                                                                                 |
| `/srv/minecraft/workshop/public/public.json`                        | controller                                                                                                                                                             | site (`BLOCKFIELD_GATE_FILE`)                     | `{mode: game/draining/workshop/maintenance, message, since}`                                                                                                                                                                                                |
| `/srv/minecraft/workshop/controller/` (0700)                        | controller                                                                                                                                                             | —                                                 | `state.json`, `members.json`, `message.json`, `controller.lock`, `inbox.lock`                                                                                                                                                                               |
| `/srv/minecraft/workshop/admin/` (0755, files 0644, `BF_ADMIN_DIR`) | controller                                                                                                                                                             | site admin and workshop server (read-only mounts) | `members.json` (current ACL), `workshop.key` (0640 root:1000, created once: the tablet's grant/revoke → site `/api/workshop/access`), `status.json` `{schema:1, revision, phase, gate, error, errorMessage, since, deadline, desired, message, updatedAt}`, |
| `session.json` `{schema:1, id, maps: [mapId]                        | null, startedAt, startedBy}`(the mod offers only these maps),`candidates.json`(below),`results/<requestId>.json` `{id, ok, error, revision, doneAt}` (newest 200 kept) |
| `/srv/minecraft/workshop/inbox/` (root:1000 0770, `BF_INBOX_DIR`)   | site admin (uid 1000, `.<id>.tmp` + rename)                                                                                                                            | controller                                        | `<id>.json` `{schema:1, id, kind: mode/members/message, actor:{draslId,name}, createdAt, ...}`                                                                                                                                                              |

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
- A backend of the _wrong_ profile appearing while STARTING is unresolved (an external restart, as above) is fenced
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

- `mode` `{profile: game|workshop|test, expectedRevision, recover?}`: same as `workshopctl request` with the current release;
  `recover` only toward game and runs the recovery step immediately. Toward workshop: `session: new|resume` (default new)
  and `maps: [mapId]|null` (null = all): a new session gets a fresh id and the chosen maps, resume keeps the previous
  session's id and maps (unless `maps` is given); stored in `controller/session.json`, exported as `admin/session.json`.
  Toward test: `candidateId`, only from phase `workshop` (see Playtest).
- `members` `{members:[{draslId,name,role,maps?}]}`: full ACL replacement (the site is the source of truth; `workshopctl members`
  is the owner fallback); a list without an `admin` is refused. `maps` is an optional array of IDs from the runtime map
  catalog, plus `hub` for building in the workshop hub; missing means all maps and the hub, unknown ids are rejected,
  and `lead`/`admin` always have all maps regardless of `maps`.
- `message` `{message}`: at most 200 characters, no control characters (Cc) or `§` (same rule as the site), stored in `controller/message.json` and exported to
  gate/public/status; an empty message returns to `BF_MAINTENANCE_MESSAGE`.

## Map candidates (candidates.py)

Builders send a map for review from the tablet (site `POST /api/workshop/submit`), admins from the site; admins accept
or reject on the site. Inbox kinds (same envelope and validation as above):

- `candidate` `{mapId, workingCopyId?, note?}`: snapshot an isolated working copy or the legacy draft
  `<BF_WORKSHOP_DATA>/world/dimensions/blockfield_workshop/<mapId>/{region,entities,poi}` plus its markup JSON into
  immutable CAS files and `<BF_CANDIDATES_DIR>/<id>.json`. No ZIP is written until an explicit export or playtest. While the workshop runs its saving is
  stopped: the controller closes entry, flushes and stops the workshop, confirms the JVM exited, then copies world and JSON. It restarts the same session and opens entry only after readiness. Save/stop/export/start failures remain closed and recoverable. Empty placeholder region files are skipped.
- `publish` `{candidateId, force?}` (the site's «Принять»): pending only, and never over a newer accepted version of the
  same map. The draft's base (`blockfield-base.json`, written by the mod when it copies a template into the workshop:
  sha256 over the sorted `<kind>/<file>\0<sha256>\n` lines of region/entities/poi, `candidates.template_digest`) must
  equal the active head's world digest (or release template if no head exists); otherwise the result is an error starting with `base changed` or `base unknown`,
  and only `force: true` accepts (recorded as `forced`). Acceptance changes **nothing** in the game: the candidate
  becomes `accepted`. An explicit export creates a checksum-verified read-only package in
  `BF_MAP_PACKAGES_DIR/<sha256>.zip` for the site's «Скачать пакет».
- `reject` `{candidateId}`. Legacy `publish` without `expectedHead` remains acceptance for the release workflow.
  Explicit activation and rollback are described below; the old rollback payload with only `mapId` is refused.
- `admin/candidates.json` `{schema:1, candidates:[{id, mapId, status: pending|accepted|rejected (older: published|
superseded|rolled_back), author, createdAt, sha256, size, regions, base, baseMatches?, decidedBy?, decidedAt?,
forced?, versionDigest?, baseVersion?, workingCopyId?}], backups, overlays, maps}`. Every version and candidate is retained. While a map has a pending
  candidate the mod keeps it read-only for everybody (it reads this file from the admin mount).

## Independent map history

The following head/CAS contract is implemented in this source tree but has not been installed or deployed.

`BF_MAP_ARCHIVE_DIR` defaults to `/mnt/media-hdd/blockfield/maps` on the homeserver HDD. It holds immutable
`blobs/<first-two-hex>/<sha256>`, `versions/<mapId>/<versionDigest>.json`, candidate metadata and derived export cache.
The host unit requires `/mnt/media-hdd` and asserts that it is a mount point; an unmounted directory cannot silently
put archives on the root SSD. Configure the host mount before enabling the unit. If using another archive mount,
change both unit paths together with `BF_MAP_ARCHIVE_DIR`, `BF_CANDIDATES_DIR` and `BF_MAP_PACKAGES_DIR`.
Changed files add blobs; unchanged files reuse hashes. A small full manifest contains `schema:1`, `mapId`,
`kind`, `parent`, author/time/source, `worldDigest`, `files: {path: sha256}`, `changed` and `deleted`.
`versionDigest` is the SHA256 of the exact manifest bytes. Parent refs belong to the same map. There is no
automatic history/ZIP deletion or binary patch chain. Existing ZIPs/releases/schematics remain intact;
`BF_LEGACY_CANDIDATES_DIR` reads old candidates without moving them, and current metadata takes precedence.

All map kinds use the same canonical layout: `map.json` and `world/{region,entities,poi}/r.X.Z.mca`, plus
`c.X.Z.mcc` when Anvil chunks need external files. Unknown sidecars are refused rather than discarded.
`kind` in map.json is `arena` (default for old configs), `lobby` or `limbo`. Schematics are historical input or
derived runtime exports, never a separate version format. Lobby/limbo conversion and runtime application
require their respective game/proxy adapters; this controller's storage support does not implement those adapters.

`BF_MAP_ACTIVE_DIR` defaults to `/srv/minecraft/maps-active` on SSD:

```text
<mapId>/head.json
<mapId>/versions/<versionDigest>/{map.json,manifest.json,world/}
drafts/<workingCopyId>/<mapId>/{map.json,manifest.json,world/}
```

Head JSON contains `schema:1`, `mapId`, `kind`, `versionDigest`, `bundle`, `generation`, `activatedAt` and
`activatedBy`. The full bundle is validated and staged before head.json is atomically replaced. `bundle` is
the controller's absolute SSD path; container adapters should resolve `<mounted-active-root>/<mapId>/versions/<versionDigest>`
from their own configured mount, rather than assume host paths exist inside containers. Capture one version
for both geometry and map.json. Mount active storage read-only in the game; never mount archive blobs writable
in Minecraft or link them into a writable world. Checkout uses ordinary copies with different inodes and writable
UID/GID 1000 files; its world carries the original version and world digest in `blockfield-base.json`.

Only active/warm bundles and editable copies are on SSD. Previous bundles stay while a backend may have captured
them. The controller removes unheaded SSD bundles only after its existing save/stop operation confirms zero full
backends, before restarting. It never removes a head bundle, a working copy or the HDD history. No cleanup runs
merely because observe() temporarily reports no process during an uncertain start.

Controller inbox contract (the same authenticated site/admin envelope and controller lock):

- `candidate {mapId, workingCopyId?, note?}` snapshots the legacy draft or the identified SSD working copy,
  producing only CAS blobs, an immutable version manifest and metadata (`native:true`, `sha256:null`, `size:null`).
  `baseVersion` tracks the actual head/template or checkout's
  pinned origin; an accepted-but-inactive candidate never becomes another draft's base.
- `publish {candidateId, expectedHead, force?}` accepts and activates. `expectedHead` is the current version digest,
  or explicit `null` if no head exists. Missing `expectedHead` preserves legacy acceptance-only behaviour.
  Stale heads, stale bases, corrupt blobs and insufficient staging space fail before head mutation.
  Acceptance and activation are distinct: if the final head write fails after acceptance, the accepted candidate
  can be retried with the same explicit expectedHead.
- `rollback {mapId, versionDigest, expectedHead}` selects that exact archived version. It does not delete newer
  versions. Missing target/expectedHead, a version from another map or a stale head is refused.
- `checkout {mapId, versionDigest, workingCopyId}` creates an isolated writable bundle; workingCopyId must be a
  canonical UUID. An existing copy is refused rather than overwritten. Resume it through the game adapter.
- `export {candidateId}` generates a legacy-format ZIP on demand in HDD `exports/<candidateId>.zip` and publishes
  a checksum-verified download in `BF_MAP_PACKAGES_DIR/<sha256>.zip`. Metadata receives `sha256`, `size`,
  `exportCompressor:zlib-1.3.2`, `exportedAt`; exported admin metadata includes `exportReady`. Export fixes ZIP timestamps
  to 1980 and pins compression to zlib 1.3.2, so an evicted ZIP regenerates exactly. Playtest generates this cache
  through `sha256_of()` before selecting its old SHA wire. Acceptance/live activation do not generate ZIPs.
  The native derived cache keeps at most 16 entries; package ownership is recorded as `derivedPackage:true` only
  when the controller creates it. Preexisting packages are not adopted for cleanup. Pruning touches only new native exports/packages, never
  legacy archives/packages, backups or CAS history. An evicted package has exportReady=false and needs a new export
  request. For the legacy release script, pass the generated `exports/<id>.zip` explicitly rather than assume a
  native candidate has a permanent ZIP beside its metadata.

`admin/maps.json` is `{schema:1, maps:[{mapId,kind,mapJson,head,versions}]}`; history entries omit the full file
table but retain version/parent/author/time/changed/deleted and fileCount. The catalog derives IDs from available
templates and stored version manifests, including lobby/limbo; the controller validates session/map ACL scopes
against that catalog. With no map store (standalone controller tests), map_ids.json remains the legacy fallback.
Runtime adapters read SSD head/bundle manifests; only host history operations read HDD blobs. The site should
mount `BF_MAP_PACKAGES_DIR` read-only and resolve downloads there rather than copy large ZIPs into the SSD admin directory.

### Isolated workshop sessions (unreleased)

In canonical mode (`active_root` configured by `workshopctl`), the host prepares every selected map before accepting
a workshop switch. A new session UUID is also its `workingCopyId`; each map is copied from its exact current head
into SSD `drafts/<uuid>/<mapId>`. Missing heads, unsafe storage, corrupt manifests/configuration or insufficient
space reject the request before drain/stop/profile selection. Resume validates existing copies and preserves their
edited region files and `map.json`; a changed active head does not silently rebase them. Session `maps:null` is frozen
to an explicit catalog list, so a later imported map does not appear in a running session automatically. An interrupted
copy may leave a complete prepared draft, never a partially exposed directory; retry preserves it.

The existing site mode API remains `mode {profile:"workshop",expectedRevision,session:"new"|"resume",maps?:[id...]}`.
The controller exports `admin/session.json {schema:1,id,maps,...}`. For native submit/create requests the site must
take `workingCopyId` from this export, match `status.json.desired.session` in phase `workshop`, check map membership
and actor ACL, and forward `candidate {mapId,workingCopyId,note?}`. The controller repeats these checks before stopping
the editor. A Java tablet submit already carries this exact ID; never replace it with a fresh or client-selected UUID.
`admin/maps.json` is the catalog/history API described above; it includes `arena`, `lobby` and `limbo` through one schema.

Workshop container configuration required before enabling canonical sessions:

| Host source                         | Container target      | Access / environment                                                                         |
| ----------------------------------- | --------------------- | -------------------------------------------------------------------------------------------- |
| `/srv/minecraft/maps-active`        | `/maps-active`        | RO; `BF_MAP_ACTIVE_DIR=/maps-active`                                                         |
| `/srv/minecraft/maps-active/drafts` | `/maps-active/drafts` | RW; `BLOCKFIELD_WORKSHOP_DRAFTS_DIR=/maps-active/drafts`                                     |
| `/srv/minecraft/workshop/admin`     | `/workshop-admin`     | RO; `BLOCKFIELD_WORKSHOP_SESSION_FILE=/workshop-admin/session.json`                          |
| `/srv/minecraft/backend-lock`       | `/backend-lock`       | RW for existing `backend.lock`; `BLOCKFIELD_BACKEND_PROFILE_FILE=/backend-lock/profile.json` |

`BLOCKFIELD_PROFILE=workshop` remains mandatory. `profile.json` is host/root-owned mode 0644 and read-only to Minecraft,
although the shared lock directory must permit the existing lock operation. Do not bind-mount `profile.json` as a single
file: atomic replacement would leave the container reading the old inode. Session exports use a directory mount too.
The adapter inspects the Swarm service specification read-only and rejects missing/incorrect mounts, writable active
storage, archive mounts or additional mounts shadowing active bundles. Configured host/container paths may differ from
these defaults, but they must identify the same physical storage. It never repairs Dokploy settings by itself.

### Offline initial imports (unreleased)

With both full backend services at zero replicas and no backend Java, import an existing Anvil template and its explicit
runtime metadata. The CLI also takes the shared backend lock; it does not stop services. All source files remain intact.

```sh
just python workshopctl.py import-map --map-id fields --world /srv/minecraft/templates/maps/fields --config /srv/minecraft/lobby/blockfield/maps/fields.json
just python workshopctl.py activate-map --map-id fields --version-digest <reported-versionDigest> --expected-head none
```

The first command records only HDD CAS blobs/manifests and reports `versionDigest`, `expectedHead`, `active:false`.
The second separately activates exactly that version; `none` is valid only before the first head. Later activation needs
the exact current digest. Arena heads can change while captured matches retain their existing bundles; lobby/limbo activation
requires stopped backends. The unreleased game adapter captures a new lobby at its next start;
head activation does not hot-swap a running lobby or regenerate/restart the proxy's runtime schematic.

Lobby source must be the stopped game app's `world/`. Supply `kind:"lobby"`, name, finite ordered X/Z bounds, empty
`supported_modes`, and `mode_data.lobby.spawn {x,y,z,yaw?}` matching the real Hub. The importer compares X/Y/Z against the
game service's `BLOCKFIELD_LOBBY_SPAWN`, default `-277.5 -40 604.5`, and checks that spawn lies inside supplied bounds.
Take bounds from the actual lobby, not an invented copy of arena metadata. Root `level.dat` and historical inputs remain
outside the canonical geometry inventory and are preserved; this is not a claim that every mod-specific sidecar is supported.

Proxy Limbo initial source is the preserved `blockfield-proxy/proxy/lobby/lobby.schem`: verified Sponge v2 / data version
3955, zero offset, 176×48×176, eight block entities and no entities/biomes. The mod's trusted bootstrap command
`bf workshop import-schematic <fresh-canonical-uuid> lobby.schem` requires an isolated workshop server with WorldEdit and
`BLOCKFIELD_WORKSHOP_IMPORT_DIR` pointing at the input directory, console/RCON permission 4, and no player entity.
It reads a bounded immutable input snapshot, validates palette/block data and positions, pastes at origin into a fresh
void `blockfield_workshop_import:<uuid>` world, flushes, emits `blockfield-import.json` with source SHA256 and output path,
and unloads without deleting the job. Stop that isolated server before importing its Anvil output with `import-map` and
explicit `kind:"limbo"` metadata. Source, failed/finished jobs, old schematics and releases are preserved. The helper is
limited to the verified zero-origin block/block-entity format; unsupported sections fail instead of being dropped.
After verifying CAS import, archive this newly created stopped job on the HDD before reclaiming its temporary SSD copy;
do not keep conversion jobs as a second permanent SSD history or remove/move the historical schematic input.

### Limbo native export and stopped selection (unreleased)

Canonical `limbo/map.json` uses `kind:"limbo"`, empty `supported_modes`, zero-origin bounds
`{minX:0,maxX:width,minZ:0,maxZ:length}` and `mode_data.limbo` with integer `width,height,length` and
`spawn:{x,y,z,yaw?,pitch?}`. Dimensions are 1–256, volume at most 2,000,000 blocks; spawn is finite and inside that volume.
For the preserved source use 176×48×176 and spawn `88.5,17,99.5`, yaw 180, pitch 0.

Configure `BF_PROXY_SERVICE` as the exact separate Swarm service and `BF_LIMBO_DERIVED_DIR` on HDD (default
`BF_MAP_ARCHIVE_DIR/limbo`). Workshop env `BLOCKFIELD_WORKSHOP_EXPORT_DIR=/limbo-export-jobs` needs exactly one
RW bind `BF_LIMBO_DERIVED_DIR/jobs:/limbo-export-jobs`; immutable archive blobs/versions remain unmounted.
The existing active-root RO mount supplies exact immutable bundles. The controller creates UUID job directories owned
by UID/GID 1000; only those jobs are writable from Minecraft.

With exactly one ready workshop backend, export an accepted candidate or trusted initial template import:

```sh
just python workshopctl.py export-limbo --version-digest <exact-versionDigest>
```

The equivalent authenticated inbox request is `kind:"limbo-export",versionDigest:<digest>` plus the ordinary
schema/id/actor/createdAt envelope. It does not switch profiles or stop the proxy. The controller writes a bounded
`jobs/<UUID>/request.json`, invokes only trusted console/RCON `bf workshop export-limbo <UUID>`, and verifies the native
result's exact version/config checksum and output checksum. The Java helper opens a private SSD Fantasy copy, rejects
native entities, custom biomes or non-air blocks outside the declared volume, and exports through existing WorldEdit.
It checks zero origin, every block state after readback and complete block-entity NBT. The current run's marker-verified
private copy is reclaimed only after server stop; unknown stale copies and failed/finished HDD jobs are preserved.

The host copies the result to immutable HDD `artifacts/<versionDigest>-<schematicSha256>.schem` and records
`artifacts/<versionDigest>.json`. Existing derivative bytes are never overwritten; a cached result is verified before reuse.
No new whole-world ZIP or separate schematic version history is created. Initial bootstrap starts workshop with selected
already-seeded arena/lobby heads, exports the staged Limbo version, then stops backends before initial `activate-map`.

Limbo publish with explicit `expectedHead`, rollback, and CLI activation require that exact verified derivative and prove
configured proxy replicas=0, no desired-running tasks and no actual service containers. CLI `activate-map` additionally
requires stopped full backends. The controller atomically writes `BF_MAP_ACTIVE_DIR/limbo/selection.json`, rechecks the
stopped proxy, then commits the head; a failed commit restores the exact previous selection while its old head still holds.
It never scales/stops/restarts the proxy. A later separately approved start uses the matching unreleased proxy reader.

Selection schema: `schema:1,mapId:"limbo",versionDigest,configSha256,schematicSha256,artifact,format:"Sponge-v2",
width,height,length,spawn`. `artifact` is exactly `<versionDigest>-<schematicSha256>.schem`, never a path. Proxy mounts
active root and `BF_LIMBO_DERIVED_DIR/artifacts` read-only; its env is
`BLOCKFIELD_MAP_ACTIVE_DIR=/maps-active`, `BLOCKFIELD_LIMBO_SELECTION_FILE=/maps-active/limbo/selection.json`,
`BLOCKFIELD_LIMBO_ARTIFACTS_DIR=/limbo-artifacts`. It binds selection to exact manifest/map JSON and refuses configured
missing/corrupt/mismatched data; unset env preserves the old image source. These adapters still need combined runtime
acceptance before production rollout; host import alone does not establish game acceptance.

Storage sources and consumers:

| Source                                                   | Writer                                | Consumer                                                                                         |
| -------------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------ |
| HDD `BF_MAP_ARCHIVE_DIR/{blobs,versions,candidates}`     | host controller under its lock        | host history, snapshot, checkout and rollback operations                                         |
| HDD `BF_MAP_PACKAGES_DIR/<sha256>.zip`                   | explicit export, then host controller | site download handler through a read-only directory mount; legacy release tooling                |
| SSD `BF_MAP_ACTIVE_DIR/<mapId>/{head.json,versions/...}` | verified host activation/rollback     | game/proxy adapters through read-only directory mounts; capture one head for config and geometry |
| SSD `BF_MAP_ACTIVE_DIR/drafts/<workingCopyId>/<mapId>`   | host checkout, then workshop editor   | workshop adapter through writable drafts storage; candidate snapshot identifies this copy        |
| `BF_ADMIN_DIR/{maps,candidates}.json`                    | host catalog export                   | site history/export controls and workshop tablet through read-only directory mounts              |

Applying a head needs the matching unreleased server adapter and pack startup changes: the currently released
image still replaces legacy templates and forces release map JSON. The game adapter captures an arena's geometry
and config for a match; it captures lobby once before readiness into a private no-save Fantasy world using exact
`mode_data.lobby.spawn {x,y,z,yaw?,pitch?}` from the same version. A newly activated lobby is selected at the next game
start, not by hot-swapping worlds. Original overworld geometry, `level.dat` and player data are not replaced; it stays
protected as the vanilla login fallback. Normal stopped cleanup removes only the current run's marker-verified
`dimensions/blockfield_lobby/<UUID>` copy, preserving unknown prior runs for stopped inspection. Proxy Limbo uses the
verified stopped selection and derivative above at its next start. Never replace live region files. Before enabling
site activation, release these readers and preserve heads across ordinary pack Deploys.

Rollout prerequisites: mount the HDD; install the host unit/env/controller as one version; give site downloads a
read-only HDD package mount; give the game a read-only active-root mount; give the workshop isolated writable drafts.
Import every runtime map and seed its initial head before enabling a strict canonical reader. Validate the complete
runtime `map.json` shape during this import: this transport checks finite ordered bounds and basic config shape,
but mode-specific gameplay metadata must pass the corresponding adapter's checks.
Do not enable site activation until captured game versions, isolated workshop sessions and lobby/limbo adapters have
passed their own checks. Existing historical releases and legacy candidates remain untouched through this rollout.

## Delivering an accepted map (release)

The legacy acceptance-only workflow delivers maps through an ordinary pack release (`v*` tag):

1. `just python scripts/maps/publish_map.py --candidates <dir with <id>.zip and <id>.json> --id <candidateId>` on a
   workstation with `gh` (copy the two files from `/srv/minecraft/workshop/candidates` or use «Скачать пакет»). It
   validates the package, uploads it as the asset `<mapId>.zip` of the private release `map-<mapId>-<sha16>` (an
   existing one is re-downloaded and compared, never replaced) and opens a PR that pins it in
   `distribution/maps.lock.json` and writes its `map.json` into `server/lobby/blockfield/maps`.
2. Merge the PR, bump the pack version and tag as usual. The release job downloads the pins
   (`scripts/maps/release_maps.py` in blockfield-server), and `build-distribution.py` replaces exactly those maps of the world seed with
   the pinned worlds after checking that the committed JSON equals the package's (never a new world with old JSON).
3. On start the game profile copies every map of the image over `/data/maps` (`start-container.sh sync_maps`; on
   prod that is the host's `/srv/minecraft/templates/maps`, which the workshop reads for drafts and base checks), so
   an existing data volume cannot keep an old world. Maps are replaced only with the backend stopped.

Rollback of one map: in blockfield-server run `just python scripts/maps/publish_map.py` with the older accepted candidate (or restore the previous pin in git) and
release. Local copy of a release's maps: `just python scripts/maps/release_maps.py` in blockfield-server (packages into `sources/maps`) or the
full server tree via `just build` in blockfield-server / `just setup` in blockfield-workspace. Hand edits of `/srv/minecraft/templates/maps` are
overwritten by the next game start: change maps through a new `world-seed-vN` or a pinned package instead.

## Playtest (profile test)

A closed test match of one candidate, in the **workshop app** (same data volume, still one backend): the site sends
`mode` `{profile: test, candidateId}` from phase `workshop`; the controller saves and stops the workshop, unpacks the
candidate into `<workshop data>/test-candidate` and starts the app with `profile.json` `profile: test`.
`start-container.sh` then runs the game profile on a fresh copy: level `test-world` from the lobby seed, all templates
of the image's release plus the candidate's world in `/data/test-maps`, its JSON as the published overlay, `BLOCKFIELD_STATS_EXPORT_ENABLED=false`,
`BLOCKFIELD_SESSION_PURPOSE=test`. The gate is `testers` (the workshop members, backend workshop). Returning with
`mode workshop` (`session: resume`) deletes the copy on start: nothing of a test reaches the drafts or the statistics.

## Workshop image deployment

The existing `.github/workflows/publish-image.yml` is named **Deploy workshop**.
Run it manually from `main` only after the owner's explicit Deploy approval.
It checks Linux/Windows, publishes `ghcr.io/blockfield/blockfield-workshop:latest`
and the immutable `:sha-<full commit>` tag, records the digest, then POSTs
`DOKPLOY_WORKSHOP_WEBHOOK` once with a 30-second timeout. Missing secrets or HTTP
failures fail the job; the webhook is never printed. A successful POST queues a
Dokploy rollout, it does not establish runtime acceptance.

Configure the existing workshop Docker application to use `:latest`, its existing
global GHCR registry, unchanged mounts/environment/healthcheck and `replicas=0`.
Store only its native deploy webhook as the repository secret; no Dokploy API key
or per-application registry credentials. Normal `main` pushes run checks only.
The controller remains an independent host-systemd installation: the image webhook
does not replace host files, change ACL/state, switch profiles or start the workshop.
After each rollout verify the resolved digest and zero replicas in Swarm separately.

## Deploys during the workshop

`deploy.yml` refuses (red job, nothing changed) while `admin/status.json` has any phase other than `game`: the game task
could not start anyway, and a new pack on the site would lock builders out of the older workshop server. Rerun the
Deploy with the same tag after returning to the game.

Defaults: `BF_CANDIDATES_DIR=/mnt/media-hdd/blockfield/maps/candidates`, `BF_MAP_BACKUPS_DIR=/mnt/media-hdd/blockfield/maps/backups` (legacy read-only history),
`BF_TEMPLATES_DIR=/srv/minecraft/templates/maps`, `BF_PUBLISHED_MAPS_DIR=/srv/minecraft/lobby/blockfield-published/maps`.

Existing backups under `/srv/minecraft/workshop/map-backups` remain preserved until an explicit, verified copy to the HDD archive. Changing the default does not move existing data. Complete that migration before switching an existing host to the new path; never delete the original as part of a controller upgrade.

## New installation (requires owner approval)

Dependencies: compatible published Blockfield server, proxy, site and launcher with
workshop readiness, gate and admin-inbox support. For an existing installation, preserve
its environment, mounts, controller state and enabled units; this bootstrap is not a migration.

1. Host (root):
   ```sh
   mountpoint -q /mnt/media-hdd       # stop here if the HDD is not mounted
   install -d -m 0755 /mnt/media-hdd/blockfield/maps /srv/minecraft/maps-active
   install -d -m 0755 /srv/minecraft/backend-lock /srv/minecraft/workshop /srv/minecraft/workshop/gate /srv/minecraft/workshop/public
   install -d -m 0700 /srv/minecraft/workshop/controller
   install -d -m 0755 /srv/minecraft/workshop/admin /srv/minecraft/workshop/admin/results
   install -d -m 0770 -o root -g 1000 /srv/minecraft/workshop/inbox   # site container uid/gid 1000 writes requests
   install -d -o 1000 -g 1000 /srv/minecraft/workshop/data
   install -d /opt/blockfield-workshop/scripts /etc/blockfield
   install -m 0644 controller.py docker_backend.py workshopctl.py journal.py candidates.py map_package.py limbo.py map_ids.json /opt/blockfield-workshop/
   install -m 0644 scripts/toolchain.py scripts/quality.py /opt/blockfield-workshop/scripts/
   install -m 0644 justfile /opt/blockfield-workshop/
   python3 /opt/blockfield-workshop/scripts/toolchain.py -c 'import sys,zlib; print(sys.version, zlib.ZLIB_RUNTIME_VERSION)'
   install -m 0600 systemd/workshop.env.example /etc/blockfield/workshop.env   # edit service names
   install -m 0644 systemd/blockfield-workshop.service systemd/blockfield-workshop.timer systemd/blockfield-workshop.path /etc/systemd/system/
   systemctl daemon-reload            # do NOT enable the timer yet
   ```
2. Dokploy `blockfield-server` (game): add bind mount `/srv/minecraft/backend-lock` -> `/backend-lock`; env
   `BLOCKFIELD_PROFILE=game`; set **rollback order stop-first** (update order must also be stop-first).
   Keep the existing `/srv/minecraft/lobby` and templates mounts (make templates `:ro`).
3. Dokploy: create `blockfield-workshop` from the independent image `ghcr.io/blockfield/blockfield-workshop:latest`, **replicas 0**,
   restart policy any, update and rollback order stop-first, same network as the proxy. Mounts:
   `/srv/minecraft/workshop/data` -> `/data`, `/srv/minecraft/templates/maps` -> `/data/maps` read-only,
   `/srv/minecraft/backend-lock` -> `/backend-lock`. Env: `BLOCKFIELD_PROFILE=workshop`,
   `BLOCKFIELD_SESSION_PURPOSE=workshop`, `BLOCKFIELD_STATS_EXPORT_ENABLED=false`, `EULA`, `TYPE`/`VERSION`/memory
   as the game app, own `RCON_PASSWORD`, same `FABRIC_PROXY_SECRET`; **no** `BLOCKFIELD_STATS_INGEST_*` variables.
   Health check as the game app (`mc-health`, interval 30s, **start period 600s**, 6 retries): the itzg image default
   (start period 120s, 2 retries) kills the workshop as unhealthy mid-startup (2026-09-23 prod smoke test).
   Put the resulting service name into `/etc/blockfield/workshop.env` and into the independent blockfield-proxy Velocity configuration (`workshop = ...`).
4. Proxy app: env `BLOCKFIELD_GATE_FILE=/workshop-gate/gate.json`, mount `/srv/minecraft/workshop/gate` -> `/workshop-gate` read-only.
   Site app: env `BLOCKFIELD_GATE_FILE=/workshop-public/public.json`, mount `/srv/minecraft/workshop/public` read-only;
   for the admin also mount `/srv/minecraft/workshop/admin` read-only and `/srv/minecraft/workshop/inbox` read-write.
5. Initialise before deploying the proxy/site with the env (with the env set and no file both fail closed):
   ```sh
   cd /opt/blockfield-workshop && set -a && . /etc/blockfield/workshop.env && set +a
   just python workshopctl.py request game --release v2.27.0   # = build in /opt/blockfield-build.json of the running game
   just python workshopctl.py step                              # expect phase game, gate public, no restart
   systemctl enable --now blockfield-workshop.timer
   ```
6. **Announced proxy restart window** (all players are disconnected once): announce in Discord/hub, wait for no running
   Ranked campaign, run the independent blockfield-proxy Deploy workflow, check `gate.json` is read (proxy log `Gate ...`) and
   that a normal login still reaches the game. Roll back by removing `BLOCKFIELD_GATE_FILE` from the proxy env.

## Operations

```sh
just python workshopctl.py members e10438e4797b49718cfecec6a8fa171b:Nether:admin <draslId>:<Name>[:builder|lead|admin[:mapId+mapId]]
just python workshopctl.py members e10438e4797b49718cfecec6a8fa171b:Nether:admin <draslId>:Builder:builder:konstaxa+fields  # restrict to two maps
just python workshopctl.py request workshop --session "$(python3 -c 'import uuid;print(uuid.uuid4())')"
just python workshopctl.py status          # the timer does the steps; phase DRAINING can last a whole campaign
just python workshopctl.py request game    # save, stop workshop, start game, open after readiness
just python workshopctl.py request game --recover   # only from ERROR; read `error` in status first
just python workshopctl.py cancel                    # abort a stuck DRAINING/STARTING switch now, no root needed
```

Drasl profile ids: `curl https://skins.blockfield.pro/account/users/profiles/minecraft/<Name>`. `members` applies online:
removed accounts are sent to the lobby on the proxy's next tick (3 s). Exit codes: 0 ok, 1 invalid request, 2 ERROR, 75 busy.

## Process journal acceptance test (owner maintenance window)

Run on the host as root, with the env file loaded, in a second terminal for the whole window:

```sh
just python journal.py record --out /root/workshop-journal-$(date +%F).jsonl   # java per app, lock holder, tasks, gate; 200 ms
just python journal.py check /root/workshop-journal-*.jsonl                  # exit 1 on any violation
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

## Still open

A draft is not re-based when its template changes (acceptance then needs `force`); resetting a draft to the template
is a manual host operation.
