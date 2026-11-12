# Workshop AI sidecar (server#321)

Java 21 service next to the workshop controller. The game JVM keeps the job state
machine, validation and apply; this sidecar owns the AI loop, provider clients,
transcribe and the money ledger. API: `docs/ai-sidecar-contract.md` in
Blockfield/blockfield-server (names there are binding).

U-W1 skeleton: `GET /health` (no auth), `POST /v1/generate`, `GET /v1/jobs/{id}`,
`POST /v1/transcribe` with a stub backend (every job completes immediately with an
empty plan), Bearer auth from the `ai-sidecar.key` file, and the SHA-256
idempotency store (same `requestId` + same body replays; different body is 409).

U-AI1 (coord#20): the money ledger (`Budget`, ported from the game JVM
`WorkshopAiBudget`) and the provider clients (`ProviderHttp`: outbound HTTP choke
point, per-call billing, tariff probe/routing, reserve math) live here now.
`POST /v1/generate` reserves `(session, builder, requestId)` in the supplier, so a
retried request never reserves twice: the stub path reserves 0 and confirms 0, the
provider path reserves the job envelope and settles the exact (authenticated or
estimated) cost. Single-shot only; the tool loop, prompt engineering, STT and the
mod call-site cutover follow in U-AI2.

Environment: `AI_SIDECAR_BIND` (default `127.0.0.1`), `AI_SIDECAR_PORT` (default
`8101`), `AI_SIDECAR_KEY_FILE` (default `/data/blockfield-workshop/ai-sidecar.key`),
`AI_SIDECAR_PROVIDER` (`stub` default, else `openrouter` | `vercel-gateway` |
`custom`), `AI_SIDECAR_PROVIDER_BASE_URL` (override), `AI_SIDECAR_PROVIDER_KEY_FILE`
(required unless stub; same key-file alphabet as `ai-sidecar.key`),
`AI_SIDECAR_POLICY_FILE` (enables the ledger), `AI_SIDECAR_LEDGER_FILE` (default
`ai-usage.json` next to the policy). No secret values in config: keys stay
env-referenced files, never logged. Without keys the sidecar serves stub plans (no
outbound traffic); a configured provider without a ledger also serves stub plans
and logs a warning — paid calls never run unledgered.

Sidecar error mapping: upstream statuses pass through (`401`/`402` unbilled
refusals settle nothing); `timeout` → `504`, ledger down → `503`, upstream-shaped
failures → `502`, exhausted grant → `402`, blocked ledger → `403`, `409` on
`requestId` reuse with a different body.

## Build, test, run

```sh
./gradlew check   # unit tests: auth + idempotency + contract shapes
./gradlew installDist
AI_SIDECAR_KEY_FILE=/data/blockfield-workshop/ai-sidecar.key ./build/install/ai-sidecar/bin/ai-sidecar
```

Environment: `AI_SIDECAR_BIND` (default `127.0.0.1`), `AI_SIDECAR_PORT` (default
`8101`), `AI_SIDECAR_KEY_FILE` (default `/data/blockfield-workshop/ai-sidecar.key`).

Client errors use `400 {phase:"request", category:"invalid_request", http:400}`;
`invalid_request` is the skeleton's validation category alongside the contract
taxonomy. `internal` outcomes are never stored: a same-key retry re-executes.

## Deploy

Image: `ai-sidecar/Dockerfile` (pinned Gradle + Temurin digests). The controller
writes `<workshop data>/blockfield-workshop/ai-sidecar.key` once at startup
(base64url, `0600 1000:1000`); see `deployment/workshop-ai.compose.example.yaml`
for the sidecar-only key mount and the game container's read-only copy.
