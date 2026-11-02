# OPAL-THINK

OPAL-THINK is the guarded reasoning service of the OPAL suite. It accepts tasks over
REST, reasons through pluggable model providers (Ollama first, OpenAI optional), and
may act on the world **only** through registered OPAL-EVENT actions — never by
executing model-generated shell/URL/Python directly. REST is the sole implementation
surface; the Typer CLI and single-file SPA communicate over HTTP only.

- Contract: `opal_think.v1.4.0.yaml` (vendored at `src/opal_think/contract/`, tested byte-identical)
- Stack: FastAPI · Pydantic v2 · Typer · HTTPX · stdlib SQLite under `var/` · asyncio engine

## Quick start

```bash
uv sync
opal-think-svc                     # serves :18100 + SPA at /
opal think health
opal think ask "what is 2+2"
```

`providers.ollama.enabled` defaults true at `http://127.0.0.1:11434` (retarget via
`opal think settings set providers.ollama.base_url '"http://HOST:11434"'`). With no
reachable provider, readiness honestly returns 503.

## REST → CLI → SPA parity (all 19 contract operations)

| REST | CLI | SPA |
|---|---|---|
| POST /tasks/ | `task create`, `ask`, `chat` | Ask form |
| GET /tasks/ | `task list` | Tasks view + status filter |
| GET /tasks/{id} | `task show` | Task detail |
| GET /tasks/{id}/stream | `task watch`, `ask`/`chat` | EventSource live output |
| POST /tasks/{id}/cancel | `task cancel` | — (button pending) |
| POST·GET·GET·DELETE /sessions | `session add/list/show/remove` | Sessions view |
| GET /models/·/models/{id} | `model list/show` | Models view |
| GET·PATCH /settings/, GET·PUT·DELETE /settings/{key} | `settings list/show/set/remove` | Settings view |
| GET /health/·live·ready | `health [--live --ready]` | Status banner |

## Semantics

- **Admission**: 202 + `Location`; Idempotency-Key replay/conflict; session-busy 409;
  `task.max_concurrent` 429; causation-depth cap 422; unknown session 404; no model 503.
- **Execution**: queued→running→terminal (`completed|failed|cancelled|timed_out`),
  `timeout_seconds` deadline, cooperative cancel, bounded `max_steps`, tool calls
  validated against the *enabled* EVENT action catalog before ever reaching EVENT.
- **SSE**: `task.started/progress/output/action/completed/failed/cancelled/timed_out`
  frames `{task_id,timestamp,type,data}`; live-only (no replay); late connect →
  terminal snapshot then close.
- **Safety**: `<think>…</think>` stripped from answers/deltas; `action_input` =
  sanitized submitted ExecutionRequest (secret redaction, 4KB cap, `None` when
  unsafe); `TaskCreate.metadata` persisted, never model-facing; secrets are
  `env:`/`ENV_VAR` refs only.
- **Retention**: startup sweep — terminal tasks older than `task.retention_days`,
  sessions idle past `session.retention_days` (cascades task history). No task DELETE.

## Evalsets

Deterministic, caller-side (contract: "no evalset management API"). YAML cases in
`evalsets/` drive the real REST surface via scripted `fake:script` generations +
`FakeEventClient`, asserting status/answer/steps and **what never reached EVENT**:

```bash
pytest tests/eval          # runs evalsets/*.yaml cases
```

## Verification status

| Surface | Deterministic | Live |
|---|---|---|
| REST (all ops), CLI, SPA | ✅ 149 tests | smoke-verified |
| Ollama | ✅ mocked transport | ✅ verified vs 192.168.1.88 (`OPAL_THINK_LIVE_OLLAMA` gated tests) |
| OpenAI | ✅ mocked transport | ❌ no credentials |
| EVENT | ✅ `FakeEventClient` | ❌ no service; execute-only auth is an EVENT 1.4.0 dependency |

## Known limitations

- Causation-depth bound covers THINK-owned ancestors only (cross-service chains invisible to THINK).
- Thinking-model `<think>` blocks are stripped from output but still cost model tokens.
- SPA `EventSource` cannot send `Authorization` headers (fine on the loopback default).
- EVENT execute-only authorization is enforced by policy on THINK's side; EVENT 1.4.0
  lacks scoped principals (documented external dependency).

## Development

```bash
pytest            # 149 tests (live-gated ones skip without env)
ruff check .
OPAL_THINK_LIVE_OLLAMA=http://host:11434 OPAL_THINK_LIVE_MODEL=smollm:360m pytest tests/integration
```
