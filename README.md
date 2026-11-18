# OPAL-THINK

OPAL-THINK is the guarded reasoning service of the OPAL suite. It accepts tasks over
REST, reasons through pluggable model providers (Ollama first, OpenAI optional), and
may act on the world **only** through registered OPAL-EVENT actions — never by
executing model-generated shell/URL/Python directly. REST is the sole implementation
surface; the Typer CLI and single-file SPA communicate over HTTP only.

- Contract: `openapi/opal_think_v1.7.1.yaml` (vendored byte-identical at `src/opal_think/contract/`)
- Stack: FastAPI · Pydantic v2 · Typer · HTTPX · stdlib SQLite under `var/` · asyncio engine

## Quick start

```bash
uv sync
opal-think-svc                     # serves :18100 + SPA at /
opal think info health
opal think ask "what is 2+2"
```

`providers.ollama.enabled` defaults true at `http://127.0.0.1:11434` (retarget via
`opal think config setting set providers.ollama.base_url '"http://HOST:11434"'`). With no
reachable provider, readiness honestly returns 503. To run the service detached,
`opal think service start|log|stop` manages a background process under `logs/`.

## REST → CLI → SPA parity (all 44 contract operations)

| REST | CLI | SPA |
|---|---|---|
| POST /task/ | `task create`, `ask`, `chat` | Ask form |
| GET /task/ | `task list` | Tasks view + status filter |
| GET /task/{id} | `task show` | Task detail |
| GET /task/{id}/stream | `task watch`, `ask`/`chat` | fetch-SSE live output |
| POST /task/{id}/cancel | `task cancel` | — (button pending) |
| POST·GET·GET·DELETE /session | `session add/list/show/remove` | Sessions view |
| GET /model/·/model/{id} | `model list/show` | Models view |
| GET·POST /persona/, GET·PATCH·DELETE /persona/{id}, POST /persona/{id}/validate | `config persona list/add/show/update/remove/validate` | Personas view |
| GET·PATCH /setting/, GET·PUT·DELETE /setting/{key} | `config setting list/update/show/set/remove` | Settings view |
| GET·POST /profile/, GET·PATCH·DELETE /profile/{id}, POST /profile/{id}/activate | `config profile list/add/show/update/remove/activate` | Profiles view |
| GET /backup/export, POST /backup/validate·/backup/import | `backup export/validate/import` | Backup view |
| GET /backup/list, POST /backup/create, GET /backup/download/{f}, POST /backup/restore/{f}, DELETE /backup/{f} | `backup list/create/download/restore/remove` | Backup view |
| POST /service/start·/service/stop, GET /service/log | `service start/stop/log` | Service view |
| GET /health/·live·ready | `info health`, `info live`, `info ready` | Status banner |
| GET /info/·/info/ping | `info version`, `info ping` | — |

## Semantics

- **Admission**: 202 + `Location`; Idempotency-Key replay/conflict resolves from
  durable state *before* any provider access (a retry succeeds with every
  provider down); session-busy 409; `task.max_concurrent` 429; causation-depth
  cap 422; unknown session 404; unknown/disabled persona 422; no model 503.
- **Action permissions**: model-only unless `event.action_allowlist` lists the
  permitted EVENT action ids; the allowlist filters both tool advertisement and
  execution-time validation, intersected with what the EVENT credential may see.
- **Execution**: queued→running→terminal (`completed|failed|cancelled|timed_out`),
  `timeout_seconds` deadline, cooperative cancel, bounded `max_steps`, tool calls
  validated against the *enabled* EVENT action catalog before ever reaching EVENT.
- **EVENT receipts**: the `execution_id` + submitted state persist on the action
  step *before* any progress event or poll, then update in place to the terminal
  outcome — one call is one durable step record. Every execute carries a stable
  caller-scoped `Idempotency-Key` (`task:call`); an ambiguous submission retries
  exactly once (EVENT ≥1.7 dedups to the same receipt; older versions at most
  one duplicate, never a retry loop; 4xx never retried). THINK-side cancel or
  timeout does **not** cancel the admitted EVENT execution — no such contract
  operation exists; the receipt stays retrievable via `GET
  /api/v1/execution/{id}` and the step notes the status honestly. Restart reaps
  the task but keeps the receipt and reports it.
- **Personas**: `TaskCreate.persona_id` selects a registered persona, else the
  session's default (`SessionCreate.persona_id`). Per-field precedence follows
  kernel `x-opal-persona`: request field > persona binding > profile > service
  default. Think's bindings are the service-local settings
  `persona.<id>.model_id` / `.instructions` / `.generation`
  (`{max_steps, timeout_seconds}`); `voice_id` remains the Say binding. The
  resolved `{persona_id, revision, snapshot}` persists on the task as
  `Task.persona` — history shows exactly which binding ran even after edits.
- **SSE**: `task.started/progress/output/action/completed/failed/cancelled/timed_out`
  frames `{task_id,timestamp,type,data}`; live frames drop under backpressure
  (no replay), but terminal frames and the stream close are *reserved* — a
  saturated watcher can never be stranded; the idle tick also reconciles the
  durable row and synthesizes a missed terminal snapshot. Late connect →
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
| EVENT | ✅ `FakeEventClient` + `mock_http_event` | ✅ real EVENT 1.7.1 booted locally by `tests/integration/test_local_event_boundary.py` (execute-scoped named principal); model simulated |

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
- EVENT execute-only authorization is enforced by EVENT ≥1.7 scoped principals
  (verified live on 1.7.1): THINK's token should be an `execute`-scope named
  principal, ideally narrowed with `actions` + `own_receipts_only`. On EVENT
  versions without scoped principals, authorization is policy-only on THINK's side.
- Cancelling or timing out a THINK task never cancels an already-admitted EVENT
  execution — EVENT exposes no cancel operation; the durable `execution_id`
  receipt stays retrievable for reconciliation.

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

When a sibling `../opal-event` checkout with a `.venv` is present (or
`OPAL_EVENT_REPO` points at one), `tests/integration/test_local_event_boundary.py`
boots a real EVENT 1.7.1 on loopback — no Ollama needed: scoped authorization,
keyed-retry single admission, chain-depth bound, durable receipt, and a watcher
observing `task.completed`, all against the live service (model simulated).

## Dotenv configuration

See [docs/DOTENV.md](docs/DOTENV.md) for `.env` loading, initialization, precedence, and `--env-file` / `--no-env-file` behavior.
