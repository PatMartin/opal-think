# OPAL-THINK

OPAL-THINK is the guarded reasoning service of the OPAL suite. It accepts tasks over
REST, reasons through pluggable model providers (Ollama first, OpenAI optional), and
may act on the world **only** through registered OPAL-EVENT actions — never by
executing model-generated shell/URL/Python directly. REST is the sole implementation
surface; the Typer CLI and single-file SPA communicate over HTTP only.

- Contract: `openapi/opal_think_v1.6.0.yaml` (vendored byte-identical at `src/opal_think/contract/`)
- Stack: FastAPI · Pydantic v2 · Typer · HTTPX · stdlib SQLite under `var/` · asyncio engine

## Quick start

```bash
uv sync
opal-think-svc                     # serves :18100 + SPA at /
opal think info health
opal think ask "what is 2+2"
```

`providers.ollama.enabled` defaults true at `http://127.0.0.1:11434` (retarget via
`opal think setting set providers.ollama.base_url '"http://HOST:11434"'`). With no
reachable provider, readiness honestly returns 503. To run the service detached,
`opal think service start|log|stop` manages a background process under `logs/`.

## REST → CLI → SPA parity (all 27 contract operations)

| REST | CLI | SPA |
|---|---|---|
| POST /task/ | `task create`, `ask`, `chat` | Ask form |
| GET /task/ | `task list` | Tasks view + status filter |
| GET /task/{id} | `task show` | Task detail |
| GET /task/{id}/stream | `task watch`, `ask`/`chat` | fetch-SSE live output |
| POST /task/{id}/cancel | `task cancel` | — (button pending) |
| POST·GET·GET·DELETE /session | `session add/list/show/remove` | Sessions view |
| GET /model/·/model/{id} | `model list/show` | Models view |
| GET·POST /persona/, GET·PATCH·DELETE /persona/{id}, POST /persona/{id}/validate | `persona list/create/show/update/remove/validate` | Personas view |
| GET·PATCH /setting/, GET·PUT·DELETE /setting/{key} | `setting list/show/set/remove` | Settings view |
| GET /health/·live·ready | `info health [--live --ready]` | Status banner |
| GET /info/·/info/ping | `info version`, `info ping` | — |

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
pytest tests/eval          # runs evalsets/*.yaml cases (live cases skip)
python scripts/run_evals.py  # writes evalsets/report.json: counts + per-case outcomes
```

Case types covered: model-only answers, guarded/forbidden actions, multi-tool
rounds, schema-rejected args, EVENT 422/5xx/unavailable, shell stdout
normalization, absolute deadlines, cooperative cancel, step-limit overflow, and
idempotent replay. `evalsets/live_ollama.yaml` is opt-in
(`OPAL_THINK_LIVE_OLLAMA` + `OPAL_THINK_LIVE_MODEL`) — report entries mark
simulated vs real-model runs distinctly with model/endpoint/git provenance.

## Verification status

| Surface | Deterministic | Live |
|---|---|---|
| REST (all ops), CLI, SPA | ✅ 149 tests | smoke-verified |
| Ollama | ✅ mocked transport | ✅ verified vs 192.168.1.88 (`OPAL_THINK_LIVE_OLLAMA` gated tests) |
| OpenAI | ✅ mocked transport | ⚠️ opt-in smoke only (`OPAL_THINK_LIVE_*`); no credentials in CI |
| EVENT | ✅ `FakeEventClient` | ❌ no service; execute-only auth is an EVENT 1.4.0 dependency |

## SPA deployment and authentication

The SPA at `/` is served by the service itself — same-origin, no build tools, REST-only
data access. Deployment notes:

- **Bearer auth**: on deployments with REST auth enabled, set the token via the
  **API token** header button (or the first 401 will prompt for it). The token is kept in
  `sessionStorage` — tab-scoped, never written to localStorage, never placed in URLs.
- **Streaming**: `GET /tasks/{id}/stream` is consumed through a fetch-based SSE reader
  (native `EventSource` cannot send `Authorization` headers); disconnects reconcile to a
  final REST snapshot.
- **CSRF**: bearer tokens are not ambient credentials — browsers do not attach them
  automatically, so cross-site request forgery does not apply to authenticated requests.
  All SPA calls are same-origin `fetch` with explicit headers.
- **Loopback default**: binding to `127.0.0.1` with auth disabled remains the default
  posture; non-loopback binds require REST auth (enforced at startup).

## Known limitations

- Causation-depth bound covers THINK-owned ancestors only (cross-service chains invisible to THINK).
- Thinking-model `<think>` blocks are stripped from output but still cost model tokens.
- The SPA is a same-origin single file: it assumes the service is served from the same origin and cannot be pointed elsewhere (cross-origin embedding is unsupported and would need CORS decisions).
- EVENT execute-only authorization is enforced by policy on THINK's side; EVENT 1.4.0
  lacks scoped principals (documented external dependency).

## Development

```bash
pytest                 # deterministic suite (live-gated tests skip without env)
ruff check .
bash scripts/verify.sh # ruff + deterministic pytest with pass/fail counts
```

Live Ollama smoke is strictly opt-in — nothing live runs unless BOTH env vars are
set. Tool tests require a model advertising the `tools` capability
(e.g. llama3.1, qwen2.5, mistral-nemo); a non-tool model skips descriptively, a
tool-capable model that emits no call fails as a capability finding. EVENT is the
in-process `mock_http_event` fixture serving one read-only diagnostic action:
real inference and wire shape, zero machine mutation.

```bash
OPAL_THINK_LIVE_OLLAMA=http://host:11434 OPAL_THINK_LIVE_MODEL=llama3.1:8b \
  pytest tests/integration -v
# knobs: OPAL_THINK_LIVE_TIMEOUT=240 OPAL_THINK_LIVE_TOOL_TRIALS=1 OPAL_THINK_LIVE_REPORT=out.json
# evidence (model tag, quantization, ollama version, per-test outcomes) is written
# to OPAL_THINK_LIVE_REPORT or $TMP/opal_think_live_smoke.json
```

Live OpenAI-compatible smoke is likewise opt-in — base URL + model + a credential
*reference* (the key value is only ever read via `env:`; possible metered API cost):

```bash
export OPENAI_API_KEY=sk-...
OPAL_THINK_LIVE_OPENAI=https://api.openai.com OPAL_THINK_LIVE_OPENAI_MODEL=gpt-4o-mini \
  pytest tests/integration/test_openai_tool_smoke.py -v
# OPAL_THINK_LIVE_OPENAI_KEY overrides which env var name holds the key (default OPENAI_API_KEY)
# evidence -> OPAL_THINK_LIVE_REPORT or $TMP/opal_think_live_openai_smoke.json
```

Live THINK↔EVENT interop is likewise opt-in — real isolated EVENT + real Ollama,
execute-scoped credential only, read-only diagnostic actions:

```bash
export OPAL_EVENT_EXECUTE_TOKEN=exec-tok
OPAL_THINK_LIVE_EVENT=http://host:8080 OPAL_THINK_LIVE_OLLAMA=http://host:11434 \
  OPAL_THINK_LIVE_MODEL=llama3.1:8b pytest tests/integration/test_live_event_interop.py -v
# knobs: OPAL_THINK_LIVE_EVENT_ACTION, OPAL_THINK_LIVE_EVENT_KEY (env var name),
#        OPAL_THINK_LIVE_TIMEOUT; evidence -> OPAL_THINK_LIVE_REPORT or $TMP
```
