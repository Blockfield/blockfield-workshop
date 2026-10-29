# Workshop controller in Dokploy: scoped contract and implementation plan

Status: proposed, documentation only. Owner scope: Workshop only (2026-10-30).
This contract preserves option C's visible, containerized Workshop controller;
it does not authorize implementation, a production credential, or a deploy.
The existing host controller remains the sole authority until an approved handover.

## 1. Scope and ownership

`Blockfield/blockfield-workshop` owns the controller, reconciliation, sessions,
map history, candidate validation and this contract. The new Dokploy application
is `blockfield-workshop-controller`; the existing `blockfield-workshop` app is
the Minecraft editor/test runtime. They are separate applications and images.
Keep one controller replica and reuse `Controller`, its revisions, locks,
operation IDs, inbox results and existing file schemas. No new general control
plane repository, database service, queue or fleet actuator is needed here.

The controller may save, sleep, wake, test, fence and select an approved release
for **Workshop only**. It may observe Game through an existing sanitized status
projection; it cannot save, stop, start, restart, update or RCON-command Game.
Workshop release selection must work without any running Game. Game admission,
client pack publication and server/client compatibility remain with their current
owners; Workshop success is not permission to publish an incompatible pair.

Excluded: host inventory, Net healer, backups redesign, proxy or storage cutover,
site rooms, runners, power policy, launcher/authentication and a general release
API. Existing site/proxy inbox and gate consumers remain compatible. A later
transport/storage change requires its own approved scope, not an incidental bind
mount or a second host helper hidden in this task.

## 2. Evidence and the dependency to remove

Baseline controller source:
[`b52eb44`](https://github.com/Blockfield/blockfield-workshop/tree/b52eb44d0230634cede10de31e822af54d505a06).
The current behavior remains documented in [OPERATIONS](OPERATIONS.md).

- [`DockerBackend.game_image()`](https://github.com/Blockfield/blockfield-workshop/blob/b52eb44d0230634cede10de31e822af54d505a06/docker_backend.py#L1247)
  resolves the sole running Game's digest; `start()` uses it. No running Game means
  Workshop cannot start on its own approved image.
- [`Controller.step()`](https://github.com/Blockfield/blockfield-workshop/blob/b52eb44d0230634cede10de31e822af54d505a06/controller.py#L1002)
  and `wake()` follow Game release observations. Replace release authority there,
  including initialization, sleeping wake and rollback, rather than bypassing one
  preflight error.
- Incident source:
  [`server/scripts/workshop-deploy.py:110`](https://github.com/Blockfield/blockfield-server/blob/46e184158da5c826f1a06f9fecb5326142ba437c/scripts/workshop-deploy.py#L110)
  raises `Pending` without public-ready Game. The client copy and both deploy
  callers also inspect live Game. Removing that check alone does not decouple
  image resolution or make whole-pair deployment safe.
- [`workshopctl._map_store()`](https://github.com/Blockfield/blockfield-workshop/blob/b52eb44d0230634cede10de31e822af54d505a06/workshopctl.py#L47)
  needs drafts, maps, archive and publication paths. Containerizing just the
  Python entry point cannot preserve these operations without a data-access
  contract.

Read-only Dokploy observations on 2026-10-29: version `v0.30.10`, no custom roles;
Workshop application ID `JrNTed6UWe6XQ25W_UEgE`, app name
`blockfield-workshop-5jx22e`, Docker provider configured with a floating server
image tag and replica metadata `1`. Its returned mounts are bind mounts.
These are configuration observations, not proof of running Java, effective
permissions or safe container storage. No production mutation was tested.

## 3. Independent release and readiness contract

Add a durable, validated Workshop release record beside existing controller state:
`{schema, revision, release, imageDigest, serverRuntime, clientContract, source}`.
`imageDigest` is the full allowlisted server repository reference with
`@sha256:<64 hex>`; `source` binds it to approved release metadata and checksums.
Never select from `latest`, a running Game, a caller-supplied arbitrary registry,
or a candidate image executed merely to extract metadata. Verify provenance,
digest and metadata consistency before accepting the record. An authenticated
operator selects a published immutable release through the existing CLI first;
adding an Actions-facing deployment API is outside this slice.

Under the existing controller lock, persist expected revision, operation ID,
previous release tuple and the new target before any external effect. Reuse
existing state/journal mechanisms; avoid a second desired-state authority.
An identical operation is idempotent; changed content under the same ID or stale
revision is rejected. Import the initial target from an operator-verified immutable
release record. Missing/ambiguous metadata blocks mutation, never triggers Game.

Report three separate facts: approved target, configured Workshop digest, and
observed Workshop READY identity. An acknowledgement must match the exact
release, runtime fingerprint, boot ID, session and generation. Never label a
configured image or an accepted deploy request as READY.

| Workshop state           | Version operation while Game is off                                                                                            |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Sleeping                 | Validate and persist target as `staged-offline`; keep Java asleep, session/maps/drafts unchanged. Wake later uses that target. |
| Stable editor            | Save/flush, close only Workshop admission, fence old runtime, apply approved digest, start and verify the same session.        |
| Test or transition       | Validate/stage target; defer activation until the operation finishes or is explicitly cancelled. Do not replace the test.      |
| ERROR or unknown runtime | Validate/stage only; activation requires explicit recovery and positive fencing. Never auto-recover Game or invent a session.  |

Deploy verification gets a distinct Workshop-only mode/result. It checks the
approved record and Workshop state even when Game is absent. Keep the existing
whole-pair verifier and public-client admission requirements separate; do not
make their success unconditional. Tests must cover both server and client copies
of the helper and their callers. Selecting Workshop while Game has another
release does not claim public pair compatibility; Workshop entry stays closed
unless the existing client/proxy contract can verify that selection safely.

Rollback selects the previously committed Workshop digest/selection in a new
operation, after fencing. Preserve session, branches, maps, drafts, usage ledger
and newer unique history. Bound compensation to one attempt; an uncertain or
failed rollback becomes `needs-operator`. Game rollback is never a controller side
effect, nor is observing Game rollback required to restore Workshop.

## 4. Concrete lifecycle mechanism and permission boundary

### Existing API calls and their limits

Use authenticated HTTPS to Dokploy's `/api`, never Docker CLI, Docker API,
`docker.sock` or a socket proxy. The sole runtime target is the fixed Workshop
application ID above. No caller can supply an application ID, URL or arbitrary
request body. A disposable environment has a different fixed mapping; it must
never inherit the production ID or credential.

The pinned
[`application` router](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/apps/dokploy/server/api/routers/application.ts#L402)
and
[`deployment` router](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/apps/dokploy/server/api/routers/deployment.ts#L41)
define these actual endpoints:

| Endpoint                               | Fixed request                                                                      | Existing permission / purpose                                                         |
| -------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `GET /application.one`                 | `applicationId=Workshop`                                                           | `service:read`; configuration only, not task termination or READY.                    |
| `GET /deployment.all`                  | `applicationId=Workshop`                                                           | `deployment:read`; deployment history, not a complete task fence.                     |
| `POST /application.stop`               | `{"applicationId":"JrNTed6UWe6XQ25W_UEgE"}`                                        | `deployment:create`; stop/scale down existing runtime.                                |
| `POST /application.start`              | Same fixed ID                                                                      | `deployment:create`; resume preconfigured runtime.                                    |
| `POST /application.saveDockerProvider` | Fixed ID, approved `dockerImage`, empty/null `username`, `password`, `registryUrl` | `service:create`; image configuration, **not acceptable to grant to the controller**. |
| `POST /application.deploy`             | Same fixed ID; bounded operation label                                             | `deployment:create`; deploy configured image, then observe asynchronously.            |

`saveDockerProvider` and generic `application.update` share `service:create`.
`application.one` returns broad application data in the pinned source; MCP
redaction does not prove API-level secret isolation. The helper
[`checkServicePermissionAndAccess`](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/packages/server/src/services/permission.ts#L244)
also makes exact identity/resource testing essential. A route allowlist in our
Python client cannot constrain a stolen Dokploy key.

### Required production mechanism

Put the narrow operation **inside Dokploy's existing trusted manager**, not a new
proxy holding an owner/admin key. Proposed endpoints, not available APIs:

- `GET /api/workshopControl.observe`: return only configured digest, desired
  replicas, template revision, task/queue identities and observed task states for
  the fixed Workshop app, including pending replacements. No env, secret, log,
  mount source or unrelated service data.
- `POST /api/workshopControl.apply` accepts only
  `{operationId, expectedRevision, action:stop|start|deploy, approvedImageDigest?}`.
  It checks organization and the fixed app, freezes/validates the sandbox template,
  serializes operations and persists deduplication before using the internal
  equivalents of the calls above. Image is optional only for `deploy` and must be
  an approved server digest. No free-form settings, commands or replicas above 1.
  Return a durable operation/task revision, never a secret-bearing app object.

The manager must fence pending old starts/deploys before acknowledging stop and
reject delayed superseded operations. Controller observation plus a held
Workshop data-volume lock and boot epoch must prove no competing writer before
copying worlds or restarting. HTTP success, `applicationStatus:done`, zero desired
replicas, heartbeat loss or failed RCON alone are insufficient. Uncertain API
results are reconciled by operation ID; retries cannot create another start.

**Minimum effective permission set:** one non-owner/non-admin identity, one
organization, one Workshop application, `observe` and `stop/start` capability;
`deploy-approved-image` only if automatic image selection is enabled. These are
the proposed narrow capabilities, not names of existing Dokploy role statements.
No access to the controller app itself, Game, proxy or another app. Grant no
generic `service:create`, `docker:read`, mounts/volumes, shell/terminal, compose,
env/secret reads or writes, registry management, schedules, builds, service
creation/deletion, role/member/API-key management or organization-wide discovery.
The custom capability must not imply ordinary broad resource reads.

For comparison, stock lifecycle calls require `service:read` and
`deployment:read/create` plus exact member resource access. This is a **test
candidate, not an approved production role**: it lacks image-only authorization,
sanitized observations and a proven queue fence. Custom action roles are an
[Enterprise feature](https://docs.dokploy.com/docs/core/enterprise/custom-roles);
their availability/license and effective token behavior have not been verified.
No combination of the observed statements establishes the full contract today.

If the manager cannot enforce this boundary, automatic image/lifecycle control
stays disabled. The controller may stage/observe in a disposable or shadow mode;
an operator may later apply an exact digest in Dokploy with separate authorization.
Do not ship a broad member/admin key as a temporary substitute. A small upstream
authorization integration may be necessary; it is a separate prerequisite, not
an unbounded platform project included in a 90-minute unit.

The eventual narrow credential lives only in the controller's private managed
secret, never Actions, site, logs or public state. Preserve the existing central
GHCR registry; do not copy credentials into application-level Docker fields.
Internal Workshop RCON connects directly on its private network with its own
password for fixed save/export commands. It is not published and has no arbitrary
console forwarding. RCON saves a running JVM; volumes carry durable state; neither
starts a stopped container or changes its image.

## 5. Container and data-access contract

Package the pinned controller/toolchain in a non-root image, read-only root where
practical, dropped capabilities, `no-new-privileges`, bounded memory/PIDs and
explicit tmpfs. Use its own event loop (15-second reconcile plus inbox polling),
not host systemd, Docker schedules or a new root helper. Preserve non-overlapping
locks and bounded steps. Health reports the controller's own state/loop and data
dependencies; sleeping Game cannot fail its health check or cause stack restarts.

Allow only managed named volumes for private controller state and established
Workshop data/projections, or a separately approved typed data API. No host binds,
bind-backed `driver_opts`, host `/proc`, devices, privileged flags, host namespaces,
Docker socket, remote Docker TLS or SSH control. Verify actual read-only write
refusal; a mount label is insufficient. An application mount implementation that
cannot enforce required RO access is a blocker; do not silently replace it with
RW access or switch the target to Compose without revisiting the app API contract.

| Dataset                                                          | Required access                                                                                                                                    |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Controller state, operation/release records, ACL/session/journal | Controller RW only; mode/UID checks.                                                                                                               |
| Admin/public/gate exports                                        | Controller owns compatible projections; existing consumers RO. Game admission remains based on passive observation, never Workshop release intent. |
| Inbox                                                            | Existing authenticated producers write requests; controller consumes with existing actor/CAS/idempotency rules.                                    |
| Selection, readiness, activity and data lock                     | Controller writes Workshop selection; runtime writes identity/READY/activity. Separate writer rights; lock spans Java lifetime.                    |
| Drafts and map history/candidates/packages                       | Existing Workshop single-writer/save windows and retention; no Game world RW mount or stats credentials.                                           |
| Catalog, published maps, settings and Game status                | RO inputs with existing producer ownership and freshness.                                                                                          |

**Current data-access gate is unresolved.** The production mounts are host binds;
the old controller also depends on HDD archive paths and host-wide Java inventory.
New private named state alone cannot give it the existing MapStore or consumer
directories. Do not copy live worlds, move archives to a default SSD volume,
provision a bind disguised as a volume, alter Game/proxy mounts, or discard map
operations to claim the migration complete. Storage/proxy cutover is outside this
slice. A compatible pre-provisioned Workshop data interface (including inbox and
projection consumers) must be supplied and verified before handover; otherwise
finish packaging and shadow evidence and keep production on the old controller.
If obtaining that interface requires storage work, return it as a separately
scoped dependency. A typed remote MapStore is not implicitly included either.

Replace host-wide PID assumptions only with managed Workshop task/queue evidence,
exclusive data locks and boot identity. Do not claim detection of every external
host JVM. Unknown potential writers block transitions and require operator
resolution. A container cannot regain that certainty by mounting host `/proc`.

## 6. Safe permission and failure test plan (future, not executed here)

1. Use an owner-authorized **disposable Dokploy installation**, version/build and
   permission model matching the intended production target. Create a harmless
   fixed-template app, a same-org canary app and a second disposable organization.
   Give them dummy data/credentials, separate named volumes and no production
   routes, networks, secrets, inbox or registry write access. Never run negative
   mutation probes against real Game/Workshop IDs.
2. Issue the proposed narrow identity there. Test the token directly, independently
   of our Python allowlist. Correctly scoped observe/stop/start/approved digest
   deploy must work; verify real tasks and immutable configured/observed identity.
   Test no-op/duplicate operation, stale revision, changed duplicate body, timeout
   followed by delayed success, concurrent deploy and stop, and manager restart.
3. Same-org canary, other org, controller app, unknown ID, arbitrary image/registry,
   tag instead of digest and extra fields must fail **with no side effect**.
   Attempt generic application update/provider change, commands/build paths,
   mounts/volumes, env/secret read/write, registry access, terminal/exec, schedules,
   compose mutation, service create/delete and identity/role edits. Use benign
   dummy fields and before/after hashes; never attempt a host escape. Any broad
   credential capability fails admission even if our client does not call it.
4. Verify stop fences active and queued old tasks, a stale start cannot resurrect
   them, and a second writer cannot obtain the data lock. Kill/restart only the
   registered disposable task via its authorized harness; test crash boundaries
   before/after journal commit, stop, image apply, start and READY. Unknown state
   blocks mutation. API outage has no Docker/SSH fallback.
5. With a separate owner-approved isolated Workshop stand, exercise Game absent,
   stopped and independently running; sleeping/stable/test/ERROR Workshop; invalid
   artifacts, lost API/RCON, stale receipts and rollback. Prove Game sees no
   lifecycle/RCON mutation. Keep a session/draft checksum and prove it survives
   wake, release change and failed start. Validate missing storage and RO refusal.
   A headless client requires its own explicit task/instance consent.
6. Save redacted request/result/status evidence, audit records, app-template
   hashes, image digests, negative-test assertions and restore results. Stop only
   owned disposable resources and revoke test credentials. Production admission
   requires all checks passing; this docs PR performs none of these writes.

## 7. Ordered Workshop-only units

Seven saved implementation units, about 60–90 minutes each. They are work slices,
not seven PRs. Stop at the stated evidence gate; external platform/data work and
owner waiting are not included in the estimate. No unit below authorizes prod.

| Order / original units         | Repo and files                                                                                                                                                                   | Local tests and isolated stand gate                                                                                                                                                                                                                   | Rollback / production step needing owner OK                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W01, 60–90m; U01/U02 parts     | Workshop: this contract, proposed `tests/dokploy_scope/` evidence harness. Dokploy manager integration is an external prerequisite.                                              | Execute §6 permission cases on a disposable installation only after authorization; record exact capability/build gap if unsupported.                                                                                                                  | Revoke disposable identity; no prod credential or write. Owner must approve the bounded manager capability before any production grant.                                                                                                                                                                                                                                         |
| W02, 75–90m; U04/U06 parts     | Workshop: `controller.py`, `workshopctl.py`, release validation helper and `test_controller*.py`, `test_workshopctl.py`.                                                         | Fail-before/pass-after: initialize, wake, stage and rollback with no Game; bad digest/metadata, CAS/replay, test/ERROR deferral; preserve session/draft checksums. Fake backend only.                                                                 | Revert offline implementation; no prod. Future activation needs approved initial immutable Workshop target.                                                                                                                                                                                                                                                                     |
| W03, 90m; U05/U06/U07 parts    | Workshop: proposed `dokploy_backend.py`, direct private-network RCON adapter, adapter tests; server `distribution/start-container.sh` only if boot/lock receipts need extension. | Fake API covers fixed app, timeouts, delayed start/fence and boot mismatch; disposable runtime proves queue fence and lifetime data lock. Reuse existing Backend protocol.                                                                            | Keep old backend authoritative; no automatic production fallback. Runtime image rollout, if needed, requires owner OK and its own repo PR.                                                                                                                                                                                                                                      |
| W04, 75–90m; U08 parts         | Workshop: proposed `Dockerfile`, container loop/entry point, `deployment/`, `workshopctl.py` path wiring and packaging tests.                                                    | Static forbidden-capability scan; pinned toolchain/zlib; non-root state writes, RO refusal, restart/single-writer and health with Game off. Use synthetic named volumes. Gate on §5 before real data.                                                 | Remove own disposable app; host untouched. New production controller app/secret/mount configuration needs owner OK. No storage/proxy migration included.                                                                                                                                                                                                                        |
| W05, 75–90m; U12 Workshop part | Server and client: `scripts/workshop-deploy.py`, `tests/test_workshop_deploy.py`, server `tests/test_deploy_workshop_guard.py`, deploy callers/docs.                             | Reproduce line-110 Pending with Game absent; new Workshop-only check stages/verifies correctly. Pair checks still reject incompatible online clients; sleeping is staged, not READY; test/ERROR defer activation. No Gradle needed for Python checks. | Revert new caller mode before enabling it. No runner redesign. Production use waits for owner approval of changed verification/release policy.                                                                                                                                                                                                                                  |
| W06, 90m; U11/U20 parts        | Workshop: existing `e2e/` harness, process journal acceptance and `docs/OPERATIONS.md`.                                                                                          | Read-only shadow from sanitized exports; no live inbox/gate writes. Then authorized isolated stand runs §6 failure matrix, session/draft preservation and old-state restore. Include server/client verifier fixtures.                                 | Stop owned shadow/stand resources; preserve evidence. No prod step; failure keeps handover blocked.                                                                                                                                                                                                                                                                             |
| W07, 60–90m; U15/U20 parts     | Workshop: `docs/OPERATIONS.md`, `systemd/` retirement runbook; workspace `docs/ARCHITECTURE.md` owner map only when integration actually changes.                                | Rehearse old/new single-writer transfer and rollback on copied data. Recheck permission and data gates; retain old unit/drop-ins, state and immutable digest.                                                                                         | **Owner-approved Workshop maintenance only:** finish in-flight operations; disable old timer/path after proving no active step; import same state/session; enable one container writer; verify wake/test/save and Game independence. Fence new writer before restoring old authority; preserve newer drafts. If data transport or gates are incompatible, stop before handover. |

U03's general auth API, U09/U10 storage/pack migration, U13/U14 site/proxy work,
U16 broad runtime/storage cutover, U17 runners and U18/U19 infrastructure work are
excluded. U07 is retained only for Workshop boot/fencing parity if existing
receipts cannot meet the contract. If any unit cannot meet its gate in its budget,
save the evidence and explicit dependency; do not add broad platform work.

## 8. Owner decisions still needed

1. **Dokploy boundary:** recommend the fixed-app, image-only capability inside the
   trusted manager, admitted through disposable negative tests. If unavailable,
   keep automatic control disabled and exact digest changes manual. Do not grant
   `service:create` or an admin token to meet the deadline.
2. **Workshop release policy:** recommend explicit selection of a verified
   published immutable server release, preserving the last approved target while
   Game is off. Retire automatic live-Game image following. Keep existing client
   compatibility/admission gates; independent staging is not public release.
3. **Data interface and handover boundary:** recommend keeping storage/proxy
   migration outside this batch and supplying a separately approved, compatible
   Workshop data interface before W07. Until then, approve only packaging/shadow
   progress. Schedule Workshop-only maintenance and explicit old-service ownership
   transfer once gates pass; no promise of zero Workshop interruption or permission
   to restart Game.

No decision on other host services, site rooms, backups or runners is needed to
review this contract. The current deliverable is documentation; implementation,
permissions, container acceptance and production handover remain unverified.
