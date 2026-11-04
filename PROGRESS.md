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

| 04 | opal-think | (pending) | main | 2026-11-04 | `pytest -q` → 176/0/3; focused 10 unit + 11 contract | ruff PASS | `normalize_execution_result` maps real EVENT Execution fields (result/stdout/stderr/exit_code/http_status/error_code/error_message) into a bounded redacted tool payload; dropped/timed_out get explanatory reasons; no fabricated fields. Live EVENT NOT RUN |
