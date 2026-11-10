# Testing OPAL-THINK with the CLI

A hands-on walkthrough of the entire service surface using `opal think`
(equivalently `opal-think`). Every command talks to the REST API over HTTP — the
CLI is a client, never a shortcut into internals — so each example exercises
real contract behavior. Examples are POSIX `bash`; swap the `export` syntax for
PowerShell as needed.

The walkthrough is ordered so that each section depends only on what came
before. It uses the bundled deterministic `fake` provider (`fake:echo`), so no
Ollama/OpenAI install or network access is required.

## Table of contents

- [1. Setup and first probes](#1-setup-and-first-probes)
- [2. A deterministic model for testing](#2-a-deterministic-model-for-testing)
- [3. Service info and health](#3-service-info-and-health)
- [4. Settings](#4-settings)
- [5. Personas](#5-personas)
- [6. Models](#6-models)
- [7. Sessions](#7-sessions)
- [8. Tasks](#8-tasks)
- [9. Streaming (SSE)](#9-streaming-sse)
- [10. ask and chat](#10-ask-and-chat)
- [11. The command tree](#11-the-command-tree)
- [12. Global CLI options and correlation headers](#12-global-cli-options-and-correlation-headers)
- [13. The SPA](#13-the-spa)
- [14. REST-only behaviors the CLI does not wrap](#14-rest-only-behaviors-the-cli-does-not-wrap)
- [15. Authentication](#15-authentication)
- [16. OPAL-EVENT integration](#16-opal-event-integration)
- [17. Automated testing](#17-automated-testing)
- [18. Troubleshooting](#18-troubleshooting)
- [19. Managing the service process](#19-managing-the-service-process)

---

## 1. Setup and first probes

```bash
# Copy: install and start the service (one terminal)
cd opal-think
uv venv && uv pip install -e ".[dev]"
opal-think-svc                      # binds 127.0.0.1:8080, SPA at /
```

```bash
# Copy: point the CLI at the service (a second terminal)
export OPAL_THINK_API_URL=http://127.0.0.1:8080

opal think info ping                     # {"status": "ok"} — process is serving
opal think info version                     # name / package version / contract api_version
opal think info health                   # status, ready, models_available, event, active_tasks
```

`opal think` and `opal-think` are the same CLI; `opal think ...` shares a root
command with sibling OPAL services when they are installed.

## 2. A deterministic model for testing

By default only the Ollama provider is enabled and pointed at
`http://127.0.0.1:11434`. With no Ollama running, `health --ready` honestly
returns 503. The built-in `fake` provider needs nothing external: `fake:echo`
echoes the last user message, `fake:script` drives scripted evals.

```bash
# Copy: enable the fake provider (dynamic provider keys materialize on first set)
opal think setting set providers.fake.type '"fake"'
opal think setting set providers.fake.enabled true
opal think setting set providers.ollama.enabled false   # optional: quiet health checks

opal think model list               # fake:echo, fake:script
opal think info health --ready           # ready: True
```

## 3. Service info and health

```bash
# Copy
opal think info version                     # {"name":"opal-think-svc","version":...,"api_version":"1.6.0"}
opal think info ping                     # {"status":"ok"} — supervisor liveness probe
opal think info health                   # full Health: status|ready|models_available|event|active_tasks|pending_restart_fields
opal think info health --live            # HealthProbe: process serving (no dependency checks)
opal think info health --ready           # HealthProbe: at least one usable model; 503 otherwise
```

## 4. Settings

Settings are the portable configuration map. Mutations are guarded by a strong
ETag (`If-Match`); the CLI auto-fetches it. Per-key deletes reset to the
registered default — there is no true deletion.

```bash
# Copy
opal think setting list                              # flat map + pending_restart_fields
opal think setting show task.max_concurrent          # value/default/is_default/pending_restart
opal think setting set task.max_concurrent 8         # values are native JSON
opal think setting set models.default_id '"fake:echo"'  # strings need quotes
opal think setting remove task.max_concurrent        # reset to default
```

To exercise the precondition machinery by hand (428 missing, 412 stale):

```bash
# Copy: manual If-Match flow with curl
ETAG=$(curl -fsSI http://127.0.0.1:8080/api/v1/setting/task.max_concurrent | tr -d '\r' | awk '/^[Ee][Tt]ag:/ {print $2}')
curl -i -X PUT http://127.0.0.1:8080/api/v1/setting/task.max_concurrent \
  -H 'Content-Type: application/json' -H "If-Match: $ETAG" -d '{"value": 8}'
curl -i -X PUT http://127.0.0.1:8080/api/v1/setting/task.max_concurrent \
  -H 'Content-Type: application/json' -d '{"value": 9}'        # -> 428 precondition_required
curl -i -X PUT http://127.0.0.1:8080/api/v1/setting/task.max_concurrent \
  -H 'Content-Type: application/json' -H 'If-Match: "s999"' -d '{"value": 9}'  # -> 412
```

## 5. Personas

Personas are named assistant identities shared across the suite; `voice_id`
optionally binds an OPAL Say voice. They live in the same portable
configuration as settings — the same `"s<n>"` ETag guards both.

```bash
# Copy
opal think persona list                                          # empty catalog first
opal think persona create amy --name "Amy" --voice-id en-gb-alan-low --description "ops assistant"
opal think persona create bob --name "Bob" --disabled            # registered but disabled
opal think persona list                                          # complete bounded catalog
opal think persona list --disabled                               # enabled-state filter
opal think persona show amy                                      # single record + ETag
opal think persona update amy --name "Amy Prime" --clear-voice   # partial update
opal think persona update bob --enabled
opal think persona validate amy                                  # registered, enabled, bindings resolve
opal think persona remove bob                                    # ETag-guarded delete
```

`validate` exits non-zero when the persona is disabled or unresolvable — useful
in scripts.

## 6. Models

Models are discovered from configured providers, never installed through the
API. IDs are opaque `provider:name` strings.

```bash
# Copy
opal think model list                     # id / provider / available / ctx / capabilities
opal think model show fake:echo           # full capability record for one model
opal think model show nope:model          # -> 404 error envelope
```

## 7. Sessions

Sessions are optional durable conversation containers. Removing one also
removes its retained task history — and is refused (409) while a task is
active.

```bash
# Copy
SID=$(opal think session add --title "demo" | awk '{print $2}')
opal think session list                   # newest-updated first, --limit/--cursor for paging
opal think session show "$SID"            # metadata + task_count
opal think session remove "$SID"          # 204; 409 if tasks are queued/running
```

## 8. Tasks

Tasks are the unit of work. `task create` waits for a terminal state by
default; `--detach` returns the durable id immediately.

```bash
# Copy
TID=$(opal think task create "say hello" --model fake:echo --detach)
opal think task show "$TID"               # status, answer, bounded step list
opal think task list                      # retained tasks, newest first
opal think task list --status completed --session-id "$SID"
opal think task cancel "$TID"             # cooperative cancel; 409 on terminal tasks
```

Useful constraints to probe: unknown `session_id` -> 404; a second active task
in the same session -> 409; more than `task.max_concurrent` active tasks ->
429; `max_steps`/`timeout_seconds` bounds -> 422.

## 9. Streaming (SSE)

`watch` follows the task's Server-Sent Events stream to its terminal event and
then shows the authoritative snapshot.

```bash
# Copy
TID=$(opal think task create "count to three" --model fake:echo --detach)
opal think task watch "$TID"              # live task.output/task.action frames, then snapshot
```

`watch` on an already-terminal task prints the stored result immediately — the
stream replays a terminal snapshot for late connects.

## 10. ask and chat

```bash
# Copy
opal think ask "what is 2+2" --model fake:echo     # create + stream + answer ("echo: what is 2+2")
opal think ask "no stream" --no-stream             # poll instead of SSE
opal think chat                                    # creates a session, loops: each turn is a task
opal think chat --session-id "$SID"                # resume an existing session
```

Exit codes: `ask`/`task create`/`watch` return 2 when the task finishes
non-`completed`, so `&&`/`||` work in scripts.

## 11. The command tree

```bash
# Copy
opal think tree              # rooted ASCII tree of every command, option, and argument
```

## 12. Global CLI options and correlation headers

Root options go before the subcommand:

```bash
# Copy
opal think --json task list                                  # exact API payload for jq piping
opal think --api-url http://127.0.0.1:8080 health            # override OPAL_THINK_API_URL
opal think --token secret123 health                          # bearer token (or OPAL_THINK_API_TOKEN)
opal think --timeout 5 health                                # client timeout seconds
opal think --opal-transaction-id tx-demo --opal-session-id sess-1 \
    --opal-causation-id task_42 task create "hello" --detach # propagate X-Opal-* context
```

The service echoes `X-Opal-Session-Id`, `X-Opal-Transaction-Id`,
`X-Opal-Request-Id`, and `X-Opal-Causation-Id` on every response (`curl -i` to
see them); a transaction id is minted when the request lacks one.

## 13. The SPA

```bash
# Copy
opal think gui                  # opens the SPA in your browser (checks the service is up first)
```

Or open <http://127.0.0.1:8080/> directly — a dependency-free single-file UI covering Ask,
Tasks, Sessions, Models, Personas, and Settings (including ETag-guarded edits).
The token button stores a bearer token in `sessionStorage` for authenticated
deployments.

## 14. REST-only behaviors the CLI does not wrap

A few contract behaviors are only visible at the HTTP boundary.

```bash
# Copy: Idempotency-Key replay (POST /api/v1/task/)
curl -i -X POST http://127.0.0.1:8080/api/v1/task/ -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' -d '{"prompt": "hello", "model_id": "fake:echo"}'
# Re-POST the exact same key+body -> the original 202 task record (replay).
# Same key with a different body -> 409 conflict.
```

```bash
# Copy: correlation headers + OpalError envelope on every response
curl -i http://127.0.0.1:8080/api/v1/task/task_missing
# -> 404 {"error":{"code":"not_found","message":...,"request_id":...,"transaction_id":...}}
#    plus X-Opal-Request-Id / X-Opal-Transaction-Id headers
```

## 15. Authentication

Loopback binds (the default) may run unauthenticated. Non-loopback binds
refuse to start without bearer auth:

```bash
# Copy: auth-enabled config (config/think.yaml)
cat > /tmp/think-auth.yaml <<'EOF'
server: {host: 127.0.0.1, port: 8081}
auth: {enabled: true, token_env: OPAL_THINK_API_TOKEN}
EOF
export OPAL_THINK_API_TOKEN=secret123
opal-think-svc --config /tmp/think-auth.yaml
```

```bash
# Copy: against the auth-enabled instance
curl -i http://127.0.0.1:8081/api/v1/info/             # -> 401 unauthorized envelope
opal think --api-url http://127.0.0.1:8081 --token secret123 info
opal think --api-url http://127.0.0.1:8081 --token wrong info   # -> error: 401
```

## 16. OPAL-EVENT integration

THINK may act on the world only through OPAL-EVENT actions (execute-scoped —
it cannot administer actions, rules, or settings). With EVENT on 1.6.0:

```bash
# Copy
opal think setting set event.base_url '"http://127.0.0.1:8080"'
opal think setting set event.credential_ref '"env:OPAL_EVENT_TOKEN"'   # secret *reference*
opal think info health            # event: available|degraded|unavailable|not_configured
opal think ask "run gpu_diagnostics" --model <a tool-capable model>
```

When `event.base_url` is unset, THINK runs model-only (no tool calls). EVENT's
422 input-schema rejections surface as tool-result errors inside the task, and
`dropped`/`timed_out` executions come back with explanatory reasons — all
visible in `task show` step records.

## 17. Automated testing

```bash
# Copy: the deterministic gate CI runs
ruff check .
pytest -q                 # ~305 tests; env-gated live tests self-skip
scripts/verify.sh         # lint + suite with VERIFY PASS/FAIL
python scripts/run_evals.py   # eval harness -> evalsets/report.json
```

Live suites are opt-in via env: `OPAL_THINK_LIVE_OLLAMA` +
`OPAL_THINK_LIVE_MODEL`, `OPAL_THINK_LIVE_OPENAI` + `OPENAI_API_KEY`, and the
THINK↔EVENT interop profile via `OPAL_THINK_LIVE_EVENT` +
`OPAL_EVENT_EXECUTE_TOKEN`.

## 18. Troubleshooting

- **`ready: False` / 503 on `--ready`** — no usable model: enable the fake
  provider (section 2) or start Ollama.
- **`refusing to start ... non-loopback`** — non-loopback binds require
  `auth.enabled`; see section 15.
- **`cannot reach OPAL-THINK API`** — service down or wrong
  `OPAL_THINK_API_URL`; `opal think info ping` is the cheapest probe.
- **`port already in use`** — rebind: `opal-think-svc --port 8081` (or
  `OPAL_THINK_PORT`).
- **412 on a mutation** — the configuration changed since your ETag was read;
  re-read and retry (the CLI auto-fetches, so this mostly hits raw-curl flows).
- **State** — everything persists in SQLite at `var/think.db`; delete it (with
  the service stopped) for a clean slate.

## 19. Managing the service process

`opal think service` runs `opal-think-svc` in the background without keeping a
terminal attached. Output overwrites `logs/opal_think.log` on each start; the
child pid is tracked in `logs/opal_think.pid`.

```bash
# Copy
opal think service start            # detached start; returns once the pidfile lands
opal think service start            # again -> stops the running service, then replaces it
opal think service log              # last 10 log lines
opal think service stop             # SIGTERM via the pidfile
```

`service start` also frees the configured port when a *foreign* process holds
it — e.g. a manually launched `opal-think-svc` — by stopping the occupant
first. `--config` selects a YAML config (or `OPAL_THINK_CONFIG`). `logs/` is
gitignored.
