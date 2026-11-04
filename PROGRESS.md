# OPAL-THINK Remediation Progress

Evidence log per the remediation pack's reporting standard. Counts are actual;
unavailable figures are marked NOT RUN/UNKNOWN.

| Step | Repo | SHA | Branch | Date | Tests (cmd → passed/failed/skipped) | Lint | Notes |
|---|---|---|---|---|---|---|---|
| 01 | opal-think | c22317d | main | 2026-11-04 | `pytest -q` → 154/0/3 (env-gated live); `pytest tests/contract/test_openapi_surface.py` → 5/0/0 | ruff PASS | CI added (lint + test 3.11/3.12 + opt-in live job); generated-vs-contract OpenAPI inventory asserted 19 ops both directions + tamper check; `scripts/verify.sh` → VERIFY PASS. CI itself NOT RUN (no remote run yet) |
| 02 | opal-think | 950ad81 | main | 2026-11-04 | `pytest -q` → 162/0/3; `pytest tests/contract/test_spa.py` → 8/0/0 | ruff PASS | All interpolated innerHTML removed (11 sites → 2 static `""` clears); untrusted values now textContent/DOM-only; static audit test enforces it; `node --check` JS syntax PASS; SPA serves live. Browser XSS e2e NOT RUN (no browser runner) — static analysis + helper routing is the guard |
| 03 | opal-think | 622e0de | main | 2026-11-04 | `pytest -q` → 170/0/3; focused 14/14 (4 unit tool-msg + 10 contract) | ruff PASS | Adapters now own vendor wire shape (`tool_call_message`/`tool_result_message`); engine assigns deterministic call_ids, every call gets a reply msg (no orphans), >4/step → explicit `action_request_limit_exceeded` result + failed step, malformed `event` → failed step w/o EVENT submit. OpenAI history now valid (assistant tool_calls + tool_call_id). Live OpenAI NOT RUN |

## Confidence (subjective, not statistical)

- Step 01: **88%** — no runtime code touched; new tests + CI config only. Uncertainty:
  workflow hasn't executed on GitHub yet (syntax verified structurally, not by a real
  run); `uv pip install --system` semantics on CI are standard but unproven here;
  the 19-op inventory test asserts the generated contract self-consistently but can't
  catch a contract edit that also removes the code.

- Step 03: **87%** — touches the engine's multi-turn loop (the most intricate path).
  Strong evidence: wire-shape unit tests per adapter, full-conversation contract tests
  asserting round-2 message structure (assistant tool_calls + matched replies),
  over-limit and malformed-arg edge coverage, 170-test regression green. Residual:
  OpenAI's exact wire expectations verified against documented shape, not a live API;
  Ollama's tolerance for extra message keys assumed per its own format; the 4-call
  bound is new behavior but contract-silent on the number.

- Step 02: **90%** — security-critical rendering refactor but statically exhaustive:
  every `innerHTML` in the file is now a static empty clear, enforced by an audit test
  that will fail the suite on any new interpolation; `node --check` proves the
  rewritten script parses; 8/8 audit tests + full regression green; live GET `/` verified.
  Residual: no browser execution (visual/interaction parity manually unchecked, and
  `EventSource`/dynamic behavior is logic-identical but not browser-confirmed); the
  `mutedRow` colspan helper and `replaceChildren` are new DOM paths tested only
  structurally. Conservative for the security surface, capped by lack of browser automation.

| 04 | opal-think | 8d4a2d3 | main | 2026-11-04 | `pytest -q` → 176/0/3; focused 10 unit + 11 contract | ruff PASS | `normalize_execution_result` maps real EVENT Execution fields (result/stdout/stderr/exit_code/http_status/error_code/error_message) into a bounded redacted tool payload; dropped/timed_out get explanatory reasons; no fabricated fields. Live EVENT NOT RUN |

| 05 | opal-think | 93f686c | main | 2026-11-04 | `pytest -q` → 180/0/3; focused 4 deadline contract | ruff PASS | One monotonic deadline from runner start consumed by every generate/EVENT submit/poll/sleep; `flag_cancel` guarded to non-terminal (single-writer); poll sleep bounded by remaining; EVENT-side-effect disclaimer documented. Live EVENT NOT RUN |

| 06 | opal-think | b850f21 | main | 2026-11-04 | `pytest -q` → 185/0/3; focused 5 capability unit | ruff PASS | `capabilities_verified` flag on DiscoveredModel; Ollama /tags + OpenAI /v1/models mark unverified (no false tool claims); `_tools_allowed` = verified-capable OR unverified→attempt; default resolution prefers verified tool-capable via bounded /api/show scan (8) when EVENT configured, else Ollama-first alphabetical. Live Ollama show-scan NOT RUN |

| 07-11 | opal-api + opal-event | 49c2b98 / baa1783 / 4d3d8fc / 434f527 | proposal/event-self-describing-actions-1.4.1 | 2026-11-04 | opal-api: structural validation only (no suite); opal-event: `pytest tests/unit tests/contract` → 178/0, full 315-test suite TIMED OUT ~20min | ruff PASS (both) | v1.4.1 contract: 4 optional self-description fields on 3 action types, byte-identical across repos; EVENT runtime: metadata round-trips, input_schema enforced at execute (422 pre-side-effect), malformed/remote-$ref rejected, jsonschema→runtime dep |

| 10 | opal-think | fd1eb30 | main | 2026-11-04 | `pytest -q` → 191/0/3; focused 17 (4 spec unit + 13 integration) | ruff PASS | `_tool_spec` consumes description/input_schema/keywords/suggested_prompts (sanitized bounded hints); input_schema maps to `data` param; client-side jsonschema pre-validation before EVENT submit; `_call` 4xx→OpalHTTPException passthrough so EVENT 422s feed back as tool results not task failures; FakeEventClient.reject_ids. Live EVENT metadata NOT RUN (server side unverified e2e) |

| 12 | opal-think | (pending) | main | 2026-11-04 | `pytest -q` → 196/0/3; focused 17 settings + 3 atomic unit | ruff PASS | `set_many_checked`/`reset_checked` run precondition + mutation in one BEGIN IMMEDIATE transaction (RevisionConflict → 412); two writers with same ETag → exactly one wins (threaded, separate handles); `set_many`/`reset` kept as unchecked wrappers. NOTE: pre-existing `uv.lock` user change in opal-event still uncommitted; opal-event security test `/bin/echo` WinError 2 is a pre-existing env failure |
