# W01: Dokploy scope evidence — BLOCKED

This is a **preflight evidence harness**, not a successful permission proof or a
production credential/backend. It makes no network calls and holds no credentials.
The existing controller remains authoritative. Automatic control cannot be admitted
from these results.

## Observed on the connected installation

The captured MCP observations are in [observed-2026-10-29.json](observed-2026-10-29.json).
No existing application was read or mutated. The only write attempt was creation
of `w01-disposable-scope-20261029`, a disposable custom role granting
`service:read`, `deployment:read/create` and `api:read`.

- `settings.getDokployVersion`: `v0.30.10`.
- `settings.getReleaseTag`: `v0.29.5`. These labels do not identify the installed
  source/image build; their disagreement remains unresolved.
- `customRole.getStatements`: broad service/deployment statements, no advertised
  Workshop-specific observe/image capability. Its `environment` entry was redacted
  by MCP; no inference is made about that entry or unadvertised endpoints.
- `customRole.all` before and after the attempt: empty lists.
- `customRole.create`: `success:false`, connector reports HTTP **403**. This proves
  that this provisioning attempt was denied. It does **not** distinguish a license
  restriction from authorization, prove every possible role configuration
  impossible, or count as a negative test of a scoped token.

No scoped identity was issued. No direct-token allow/deny cases or runtime
deploy/start/stop/restart tests ran. No admin/member key was substituted. The
connected installation is shared; no separate disposable Dokploy installation was
supplied for manager-restart/global-mutation failure tests. Authorization for
named disposable resources does not authorize restarting the shared manager or
changing existing server settings. The blocked preflight is preserved rather than
extending the test into those operations.

## Pinned-source gap, not installed-build proof

The source model uses upstream Dokploy commit
[`073713f0b863eca4285ad24db00550af1b39397a`](https://github.com/Dokploy/dokploy/tree/073713f0b863eca4285ad24db00550af1b39397a).
Paths and whole-file SHA-256 checksums are in the evidence file. This source was
downloaded read-only and checked locally; its identity with the installed build
has **not** been established.

- [`application.saveDockerProvider` and `application.update`](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/apps/dokploy/server/api/routers/application.ts#L644)
  both require `service:create` through the same application-access helper.
  A permission grant enabling image configuration also enables generic edits
  on the granted app. One-app resource scoping cannot separate these actions.
- [`application.one`](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/apps/dokploy/server/api/routers/application.ts#L255)
  returns broad application data. MCP redaction does not establish raw-API
  secret isolation. Read access alone is not the proposed sanitized observation.
- The [`member` base role](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/packages/server/src/lib/access-control.ts#L169)
  grants env, volume and schedule operations within accessible services.
  Legacy creation/access flags do not remove those base grants.
- [Custom-role resolution](https://github.com/Dokploy/dokploy/blob/073713f0b863eca4285ad24db00550af1b39397a/packages/server/src/services/permission.ts#L39)
  is license gated. The observed 403 alone does not prove the installed license
  state.

The safe alternative is the bounded capability inside Dokploy's trusted manager
specified by Workshop control-plane contract §4: a fixed-app sanitized observer
and serialized stop/start/approved-digest deployment, with sandbox validation,
durable operation IDs/revision checks and queue/task fencing. Granting broad
`service:create`, a member/admin key, Docker access or a proxy carrying an admin
key is not a fallback. Until that capability and its effective token boundary are
proven, keep automatic control disabled; stage only, with a separately authorized
operator applying a verified immutable digest if needed.

## Reproduce the evidence checks

From the Workshop repository (the preflight script uses only Python's standard
library; the unit checks use the repository's pinned toolchain):

```sh
just python -m unittest discover -s tests/dokploy_scope -t . -v
python tests/dokploy_scope/preflight.py
```

The second command deliberately exits **2** with `state: BLOCKED` and
`production_admission: false`. Exit **1** means invalid/unreadable evidence. There
is no success/admission exit for preflight data. Offline unit-test success checks
the harness's handling of the observations, not Dokploy's permissions.

To check the source evidence too, download the three files listed under
`source_model.files` from the full upstream commit above into an empty local
directory, preserving each basename. Downloads are data, not executable setup;
there is no network access on import. Then run:

```sh
python tests/dokploy_scope/preflight.py --source-root /path/to/pinned-source
```

The harness verifies each SHA-256 and the two image/generic-edit permission checks
before returning the same blocked result. It emits selected booleans/counts only,
never raw responses or exception text.

## Remaining acceptance and cleanup

Contract §6 still needs a matching **disposable installation**, a provisionable
non-admin identity and the bounded manager capability. Use dummy target, same-org
canary, controller and foreign-org apps; direct token requests must bypass our
client allowlist. Prove real task identity, immutable images, required lifecycle
operations and every forbidden surface, with before/after hashes and audit data.
Then exercise replay/CAS, timeout/delayed success, concurrent deploy/stop, queued
old starts, manager restart and lifetime writer-lock/crash boundaries. The
separately authorized Workshop/Game/data matrix and headless-client consent remain
separate requirements. `preflight.PENDING` enumerates the unrun groups; none is
counted as a pass or silently skipped as successful acceptance.

Cleanup inventory: **zero created resources and zero issued credentials**. The
only attempted resource was the named custom role; a successful post-attempt
`customRole.all` returned `[]`, confirming absence. Nothing needed deletion or
revocation. No app, project, organization, volume, network, route, registry, VM,
server, client, daemon or watcher was created. Downloaded source copies are
temporary local evidence inputs, removed after verification. No `BF_DEV_HOME` was
created.
