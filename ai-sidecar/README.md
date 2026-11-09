# Workshop AI sidecar (server#321)

Java 21 service next to the workshop controller. The game JVM keeps the job state
machine, validation and apply; this sidecar owns the AI loop, provider clients,
transcribe and the money ledger. API: `docs/ai-sidecar-contract.md` in
Blockfield/blockfield-server (names there are binding).

U-W1 skeleton: `GET /health` (no auth), `POST /v1/generate`, `GET /v1/jobs/{id}`,
`POST /v1/transcribe` with a stub backend (every job completes immediately with an
empty plan), Bearer auth from the `ai-sidecar.key` file, and the SHA-256
idempotency store (same `requestId` + same body replays; different body is 409).
Provider code moves here in U-S3/U-W2.

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
