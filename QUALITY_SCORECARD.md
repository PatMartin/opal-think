# OPAL-THINK Quality Scorecard — Phase 2 (2026-11-05)

Rubric: weighted categories, each graded 0–5 in 0.5 steps; score = Σ(weight × grade/5).
Baseline at Phase-2 planning: **78/100** (217 pass / 4 skip, Ruff clean). This is a
reported engineering assessment, not a zero-bug probability.

| Category | Weight | Grade | Weighted | Key evidence | Residual unknowns |
|---|---|---|---|---|---|
| REST contract | 15 | 4.5 | 13.5 | Vendored 1.4.0 contract + parity tests; opal-api `opal_event.v1.4.1.yaml` published with structural-parity tests (33 assertions) and byte-identical sync guard in opal-event (sha256 `033aed32…`) | Proposal branches not yet merged to main; contract changes await ratification |
| Task engine | 20 | 4.0 | 16.0 | Real HTTP-boundary tests vs released EVENT surface; protocol-violation guards (malformed admission/unknown status fail fast); bounded lexical tool selection (16 specs/8KiB); deterministic context budgeting with protected tool pairs; per-task telemetry | Selection recall unmeasured on real catalogs; chars/4 token estimate is approximate |
| Model providers | 10 | 3.5 | 7.0 | OpenAI capability-vs-outage taxonomy (`unsupported_capability` ≠ `unavailable`); malformed/refusal/stream-error handling + regression tests; opt-in Ollama/OpenAI live smokes verified against emulated endpoints incl. negative paths | Zero real-model runs in this environment; provider-specific quirks unobserved |
| EVENT integration | 15 | 4.0 | 12.0 | Server-authoritative `input_schema` → 422 before admission (16KiB cap, local $refs, all 3 action types, 29 contract tests); execute-scoped credentials with discovery projection hiding operational fields; `execution_id` persisted before wait | Live THINK↔EVENT profile written but never executed; projection/scope untested by a real client |
| Persistence | 10 | 4.0 | 8.0 | Atomic settings writes; orphan reaping now preserves last EVENT `execution_id` in failure error (operator can inspect downstream); no auto-resubmit of admitted actions | Restart semantics verified at store level; process-crash mid-write path not fault-injected |
| CLI/SPA | 10 | 3.5 | 7.0 | CLI parity suite green (28); SPA `node --check` on real script blocks; opt-in Playwright suite written against real DOM ids | Browser smoke never executed — playwright/chromium unavailable (documented gap) |
| Tests/evals | 10 | 4.5 | 9.0 | 275 deterministic pass / 17 live-gated skips; mock_http_event released-contract fixture; evalsets with git SHA/python/platform/live flags + evalsets digest | Eval precision/recall metrics not computed; eval report not continuously regenerated |
| Ops/observability | 5 | 3.5 | 3.5 | Structured per-task telemetry (counts, usage, duration) on terminal SSE + logs; correlation headers throughout | No metrics endpoint/aggregation (deferred — contract field needs separate approval) |
| Documentation | 5 | 4.0 | 4.0 | README opt-in live instructions (Ollama/OpenAI/EVENT interop/browser); per-step evidence in spec/PROGRESS.md; honest skip semantics | Live sections untested by a second operator |

**Total: 80/100** (baseline 78 → +2)

## What Phase 2 actually changed

- **Deterministic boundary proof** (step 01): integration tests now cross the real
  HTTP adapter — previously injected fakes; +14 boundary tests, protocol guards.
- **Live readiness** (02/03/08/12): five env-gated profiles for Ollama, OpenAI,
  THINK↔EVENT interop, and browser — each verified to collect/skip correctly and
  dry-run against emulated endpoints. **None executed against real services.**
- **EVENT contract + enforcement** (04–07): versioned 1.4.1 self-describing
  actions published centrally, byte-identical downstream, execute-time schema
  validation (422 before admission), execute-scoped auth + discovery projection.
- **Runtime robustness** (09–11, 13): bounded tool catalog, context budgeting,
  task telemetry, honest restart semantics with preserved execution links.

## Honest limitations

- Zero real provider/EVENT/browser executions — all "live" evidence is
  emulated-endpoint or skip-verified. This is the single largest residual risk.
- The EVENT work sits on unmerged proposal branches in two repos.
- opal-event's full regression suite exceeded practical time twice (>23m,
  >45m) — bounded 196-test contract+unit evidence was used instead.
- Score reflects source+test evidence; it is not a defect-free claim.

_Generated as the step-13 deliverable; update only with measured evidence._
