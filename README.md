# Workshop safety core — not a deployed workshop

Refs netherg-io/blockfield-releases#8; draining follows #6. Python 3.11+ on Linux. No third-party Python packages.

```sh
python3 -m unittest discover -s tools/workshop -p 'test_*.py' -v
python3 tools/workshop/map_package.py candidate.zip --sha256 <pinned-archive-sha256>
```

22 local unittest methods pass: 13 controller tests against an explicit fake Backend and 9 package tests (some parameterized subcases). These are NOT live Docker/Minecraft acceptance tests. Package region bytes are explicitly synthetic fixtures, not a valid playable world. Validation checks packaging/identity/hashes, not Anvil/NBT integrity, spawn safety, supported blocks or game balance.

## Implemented controller core

Controller.request(operation UUID, Goal, expected_revision) durably stores a closed-gate intent using fsync/atomic replacement. Reused current operations are idempotent, conflicting payloads and stale previous operations are rejected using compare-and-swap revisions. A nonblocking process file lock serializes controller instances. Missing state starts closed; unknown schemas are rejected. State directory must be local persistent storage, private to the service account, outside all Minecraft worlds and release trees.

step() makes one reconciliation, not a background thread. A complete game campaign must drain before switching; repeated DRAINING never becomes an implicit forced abort. The adapter must include durable local result/outbox completion in drained(), not wait forever for a remote statistics HTTP ACK. Existing ranked rounds and valid reconnects continue during DRAINING; no new campaign may start. Casual finishes its current battle.

Every switch closes admission, saves/freezes the old profile, stops it, and verifies there are zero remaining full backends before starting the new profile. start() returning is NOT readiness. Reconciliation waits for observed matching profile/session/release readiness; only then selects public/builders/testers admission. A save/stop/start error stays closed. ERROR requires explicit step(recover=True). Test goals require an immutable candidate SHA256; a normal game goal cannot contain a workshop session/candidate. Returning to a previously pinned game release never publishes a draft.

## Mandatory runtime integration still absent

Backend is a Protocol and tests supply FakeBackend. **No Docker adapter, proxy gate, in-game drain/save API, web ACL or scheduler is installed.** The production deployment/restart policies remain unchanged. Passing these tests does not prove the production host cannot start two backends.

Before enabling: implement a tightly allowlisted adapter that fences EVERY restart/deploy/host-boot path, inventories actual full processes (including stopping/unready ones), verifies separate draft/template/live mounts, waits for actual process exit, and provides session/release readiness. Never grant builders Docker/RCON/OP. Do not pass arbitrary shell commands from web requests. Existing Velocity/Drasl/Limbo must enforce authenticated-account membership for initial join, reconnect and backend switches; nickname whitelist is not authorization. Public API must not expose this private state document or session IDs. Missing/stale/corrupt/unavailable gate/ACL data must fail closed; add a freshness/lease contract before using the persisted gate as an authority.

Implement session/map/role storage, WorldEdit boundary enforcement including deferred operations/undo, existing deployment coordination, status/UI, consistent offline snapshots, immutable candidates/review, playtest copies, publisher and atomic activation/rollback. Drafts must never live under MatchWorld.cleanupStale. Only one heavy backend, including tests/exports/recovery. Multi-client, crash/power-loss, disk-full, actual process-count/RAM and modded-world tests remain open.

## Candidate layout v1

A ZIP contains manifest.json, map.json and world/{region,entities,poi}/r.X.Z.mca. manifest.json has schemaVersion=1, mapId, version, baseVersion and files mapping every non-manifest regular file to its SHA256. map.json.id must match mapId. External archive SHA256 is mandatory; it binds the manifest too. At least one world/region file is required.

The read-only validator copies bounded compressed bytes to private temporary scratch, then checks that exact pinned snapshot, path canonicality, duplicates, special/symlink/encrypted files, permitted layout, expanded/file-count/metadata limits, every member digest, finite/unambiguous JSON and world/config identity. It never extracts into a world, publishes, deletes, or changes a release pointer. Scratch is deleted on close.

Unknown mod sidecars are rejected, NOT silently omitted. Full export compatibility is not established by region/entities/poi alone. Extend the allowlist/schema after testing real mod-dependent data. There is intentionally no archive builder that would pretend a copy of actively changing region files is a consistent backup. Large datasets and ZIP central-directory memory behaviour require additional load testing.

Existing packwiz pins, server/config/world data, start-container.sh, production workflows and repository visibility are untouched. No release, deployment, main write or merge is part of this PR.
