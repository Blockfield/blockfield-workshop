# Workshop E2E stand

The harness drives the real `workshopctl` and controller against native, registered
server JVMs. Game and Workshop run together, each with its own data, selection and
process record. The controller manages Workshop/editor and candidate-test restarts;
Game starts only through the explicit stand bootstrap API and stays running during
those operations. Production still uses the Docker adapter in
[OPERATIONS](../docs/OPERATIONS.md).

Use the workspace's owned stand and `just` for ordinary game acceptance. This adapter
is for controller integration tests requiring the native equivalent of two services.
It does not launch a client or provide desktop automation. Observe process ownership
and resource limits from the workspace AGENTS.md before any build or server start.

## Environment

| Variable                                 | Default                                                             | Meaning                                                          |
| ---------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `BF_WORKSPACE`                           | sibling `blockfield-workspace`                                      | workspace containing the mod rig and shared process/RCON helpers |
| `STAND_DIR`                              | `$XDG_CACHE_HOME/bf-workshop-stand` or `~/.cache/bf-workshop-stand` | absolute path to this session's isolated stand                   |
| Native agent session ID / `BF_RIG_OWNER` | required                                                            | helper owner; an override must match the detected native session |
| `JAVA`                                   | `JAVA_HOME/bin/java` or native `java` on PATH                       | existing JDK 21                                                  |
| `STAND_XMX`                              | `1536m`                                                             | maximum heap per JVM, with native memory additional              |
| `STAND_TEST_SEED`                        | `STAND_DIR/seed/world`                                              | immutable, offline packed lobby seed for candidate tests         |

Owner detection follows `agent-process`: `AI_AGENT_SESSION_ID`, `CODEX_THREAD_ID`,
`CODEX_SESSION_ID`, `CLAUDE_SESSION_ID`, `GEMINI_SESSION_ID`, `OPENCODE_SESSION_ID`,
then `CLAUDE_CODE_SESSION_ID`. A mismatched `BF_RIG_OWNER` is refused. With no native
context, the operator must explicitly set `BF_RIG_OWNER`.

Additional game/Workshop environment settings can be provided in
`game-env.json` / `workshop-env.json`. Keep credentials out of logs and version
control. Controller exports, canonical draft paths, archive and active map storage
are isolated under `STAND_DIR`; the adapter sets their native paths explicitly.

## Bootstrap

Prepare two copies of an **offline packed server**, with the same pinned release.
Use a fresh stand directory. Never overlay an existing Workshop or copy a running
Game world. Preserve a separate immutable seed before the first Game launch:

```sh
test ! -e "$STAND_DIR/game" && test ! -e "$STAND_DIR/workshop" && test ! -e "$STAND_DIR/seed" || exit 1
mkdir -p "$STAND_DIR/seed" "$STAND_DIR/lock"
cp -a "$OFFLINE_SERVER_PACK" "$STAND_DIR/game"
cp -a "$OFFLINE_SERVER_PACK" "$STAND_DIR/workshop"
cp -a "$OFFLINE_SERVER_PACK/world" "$STAND_DIR/seed/world"
```

Configure four distinct ports in the two `server.properties` files, for example:

| App                     | `server-port` | `rcon.port` |
| ----------------------- | ------------- | ----------- |
| Game                    | 25590         | 25580       |
| Workshop/editor or test | 25591         | 25581       |

Both apps need `enable-rcon=true` and their own configured `rcon.password`. The
adapter reads the target's properties; it verifies that the RCON listener belongs
to that exact owned JVM before sending a command. Discover listeners, commands and
working directories before choosing these ports. An occupied configured port is
an error; the adapter never chooses a replacement port.

Import the initial immutable map versions and activate their heads **while both
backends are stopped**, using the existing `workshopctl` map commands. Canonical
sessions require those heads before preparing a working copy. Do not alter existing
drafts to make bootstrap succeed.

Start Game explicitly, then let the controller reconcile the persistent editor:

```sh
python3 e2e/stand.py bootstrap-game <exact-release>
python3 e2e/stand.py step
python3 e2e/stand.py members <adminDraslId>:Nether:admin <builderDraslId>:Builder:builder:fields
python3 e2e/stand.py request workshop --session <canonical-session-uuid>
bash e2e/settle.sh workshop
```

Selection files are `lock/game/profile.json` and `lock/workshop/profile.json`.
Each has its own fresh generation; readiness must match profile, generation,
session and release. A legacy `lock/profile.json` is read only for its matching app.
Process receipts are `game/rig-process.json` and `workshop/rig-process.json`.
A legacy root receipt migrates only after its app, helper owner and exact native
identity have been verified. Foreign or unregistered processes are never stopped.

## Acceptance scenarios

Record a bounded journal while exercising the controller:

```sh
python3 e2e/journal_stand.py record "$STAND_DIR/evidence/journal.jsonl" 600
python3 e2e/journal_stand.py check "$STAND_DIR/evidence/journal.jsonl"
```

The invariant is at most one Game JVM plus at most one Workshop/test JVM. Unknown
or duplicate backends and incomplete samples fail the check. Each open gate needs
its own exact ready profile: Game `public`/`draining`, editor `builders`, candidate
`testers`. Closing or restarting Workshop must leave ready Game public. Legacy
journals retain their historical exclusive interpretation.
Every Java process in the exact Game/Workshop working directories is counted,
including alternate launcher names. A process without matching owned registration,
current command, PID birth and cwd is recorded as unknown and fails the journal;
an occupied app directory also blocks starting a replacement.

Exercise these behaviors on the owned stand:

1. Resume the same editor session with its immutable map/branch selection and change
   member ACLs. Select a new session UUID when changing maps or branches; the old
   session's working copies and dirty world files must remain available for resume.
2. Snapshot a permitted draft, test its immutable candidate, then resume the editor.
   The test copy comes from the offline lobby seed; Game and persistent drafts stay
   unchanged. A test release must match ready Game.
3. Revoke a builder while Game players remain connected. Only the revoked builder
   returns to Limbo; WorldEdit edits and privileged commands must remain gated.
4. Submit concurrent requests with the same revision. One wins the existing CAS;
   the other receives a stale-revision error. No duplicate JVM starts.
5. Inject `preflight`, `save`, `stop`, `start` or `start_crash` in `faults.json`, using
   `true` or `"once"`. Inspect `errorMessage`, then recover through the existing
   controller request. Workshop failure must leave Game unchanged.
6. Check both virtual hosts through the real proxy and an isolated Drasl fixture.
   Game goes only to Game, allowed Workshop accounts only to Workshop, and other
   accounts stay in Limbo. Test TCP/Raknet separately when enabled. A direct backend
   connection must retain its Velocity requirement.

The adapter saves Workshop before stopping and uses only
`agent-process stop --owner <current-session> --grace-seconds 120 --no-escalate <exact-receipt-id>`
after rechecking native PID, birth, executable and cwd. A timeout reports failure,
keeps the live ownership receipt and refuses a replacement JVM; it never escalates
to SIGKILL. The helper call has a 130-second deadline. It never sends RCON `stop`. To exercise a
registered Workshop restart without touching Game, call
`StandBackend().stop(process)` for the observed Workshop, then reconcile again.
Cleanup must also use exact owned receipts; this controller deliberately has no
Game-stop API.

## Consumption

Journal rows contain `apps.game` / `apps.workshop` with PID and RSS, per-app heap
samples about every five seconds, and total `rss_mb`. Compare empty Workshop,
builder activity and candidate tests while Game stays up. `STAND_XMX` limits heap,
not total RSS; account for native memory and the two JVMs together.

```sh
python3 e2e/journal_stand.py record "$STAND_DIR/evidence/memory.jsonl" 600
```

Disk-full and Docker/host restarts remain production-specific checks. This mock
suite and native adapter do not establish production consumption or gameplay
acceptance; measure those through the actual deployed services.
