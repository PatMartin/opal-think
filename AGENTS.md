# AGENTS.md — OPAL THINK

## Purpose

OPAL THINK is the **model-neutral reasoning and task-execution coordinator** in the OPAL ecosystem. It accepts intelligent work, selects an appropriate model, manages bounded task execution, and—only when authorized—requests actions through the designated OPAL action-execution service.

THINK must remain usable as a standalone reasoning service. The REST API is the authoritative interface; CLI and web clients consume it rather than implementing separate business logic.

These are **ongoing engineering guardrails**, not a migration plan. Do not initiate contract-version reconciliation, broad refactors, or unrelated feature work merely to satisfy this file.

## OPAL design tenets

1. **Independent by default.** Each module works meaningfully on its own. Integration with other OPAL services is optional unless a specific operation inherently needs them.
2. **Clear responsibilities.** Keep sensing in LISTEN/LOOK, speech generation in SAY, transport in BUS, action execution in EVENT or its approved successor, and reasoning/task coordination in THINK.
3. **Contract-first interoperability.** OpenAPI defines public behavior. Shared conventions belong in the OPAL kernel; services add only their distinct capabilities.
4. **One behavior, multiple interfaces.** REST, CLI, and web UI express the same operations and semantics. Avoid independent implementations and feature drift.
5. **Pluggable implementations.** Models, providers, transports, and adapters can change without changing the meaning of the public operation.
6. **Local-first, deployable anywhere.** Prefer local operation, modest dependencies, explicit configuration, and realistic resource limits; remote services are optional adapters.
7. **Simple, observable, and bounded.** Favor the smallest understandable design, stable identities, explicit state transitions, diagnosable errors, and bounded work. Avoid hidden global state and unbounded queues/retries.
8. **Extend the right surface.** If a capability performs an existing operation differently, extend its parameters. If it introduces meaningfully different behavior, extend the API deliberately. Do not create parallel APIs for superficial variations.

When a proposed change conflicts with one of these principles, explain the tradeoff before implementing it. Do not add abstractions simply because they may be useful later.

## Ownership and boundaries

- A **Task** is the universal unit of intelligent work. `ask` and `chat` are client conveniences over task creation, not separate reasoning engines or independent REST APIs.
- A **Session** is optional managed conversation context; do not implicitly create a persistent session for a standalone task. Distinguish actual session resources from correlation/session headers.
- THINK owns task admission, model selection, reasoning-loop coordination, cancellation requests, progress reporting, and retained task records.
- THINK does **not** own microphone/camera capture, speech synthesis, physical playback, generic event transport, action definitions, rule evaluation, or execution of side-effecting actions.
- Do not turn THINK into a general workflow engine, tool registry, device registry, event broker, or distributed scheduler without an explicitly approved architectural decision.
- Do not make Ollama, OpenAI, EVENT, BUS, SAY, or other sibling services mandatory for basic contract inspection and appropriate standalone testing. The ability to perform a particular task may legitimately depend on an available model or action service.

## Models, providers, and reasoning

- Keep provider adapters behind a small common interface. Preserve a normalized model catalog, capability discovery, execution results, and error semantics.
- Support local providers such as Ollama without requiring cloud credentials; remote providers are optional and must use explicit configuration.
- Select models according to declared capabilities and availability. Never silently switch models, grant capabilities, or change execution policy in ways that affect correctness or safety.
- Keep model-native settings and messages inside provider adapters where possible. Do not leak vendor wire formats into public OPAL contracts.
- Validate model/tool-call output as **untrusted input**. Malformed arguments, unexpected content, unsupported capabilities, and model errors must fail predictably.
- Do not expose private chain-of-thought, provider secrets, hidden system prompts, or credentials in task history, logs, streamed responses, or action calls. User-visible summaries may explain outcomes and reference actions without exposing private reasoning.
- Maintain deterministic fake/mock providers for meaningful tests that require neither a network nor real model weights.

## Task lifecycle and durable state

- Keep task states explicit and consistent, including queued/running and terminal completed/failed/cancelled/timed-out outcomes as defined by the authoritative contract.
- Async admission means **accepted**, not **completed**. Return the contract-defined status and location, then expose durable task status independently of a live connection.
- Enforce finite timeouts, concurrency limits, queue limits, reasoning-step limits, and any configured resource budgets on the **server**, not merely in the CLI.
- Cooperative cancellation must not imply successful cancellation of already-admitted external side effects.
- Preserve task identity, provenance, selected model/persona snapshot where defined, bounded step summaries, timestamps, and terminal outcome across process restarts according to the persistence contract.
- Treat persistence failures, orphaned running tasks, restart recovery, and retention as first-class correctness cases. Do not quietly report incomplete work as completed.
- Ensure tasks do not accidentally overwrite one another's state or share mutable execution context.

## Action execution and EVENT boundary

- **Never directly execute model-generated shell commands, Python, arbitrary HTTP requests, or other side-effecting operations from THINK.** Model-requested actions must go through the designated authorized action-execution interface.
- Obtain available actions from that interface; validate enabled state, declared schemas, the calling principal's permissions, and THINK's configured policy **both before advertisement and immediately before execution**.
- Use least-privilege, execution-scoped credentials. THINK must not need permissions to administer other services' actions, rules, settings, or secrets.
- Do not invent a second independent action registry, approval framework, or policy engine when the existing execution boundary already owns those responsibilities.
- Persist execution identifiers and submission state before treating external work as durable progress. Reconcile outcomes using the execution service's authoritative receipts.
- Distinguish submission, acceptance, running, completion, failure, drop, and timeout. A successful HTTP admission is not proof that an action succeeded.
- Retrying an action can duplicate side effects. Only use bounded, documented, idempotency-aware retries when the downstream contract supports them; never promise exactly-once behavior without an actual guarantee.
- Bound recursive feedback and causation depth across THINK-to-action-service-to-THINK cycles. Propagate correlation metadata and reject unsafe cycles rather than hoping local step limits suffice.
- The BUS transport layer must not be assumed to provide action execution merely because events travel through it.

## Streaming and client behavior

- Task streaming uses the contract-defined event stream (SSE where applicable). Preserve event names, payload shapes, content types, and correlation headers.
- Treat streams as **live views**, not guaranteed durable replay. The persisted task record is the authoritative source for final status and output.
- Make terminal status observable even when a client was slow, disconnected, or joined late. Handle backpressure without unbounded memory and without leaving clients waiting indefinitely.
- Never confuse a dropped progress event with a dropped task or a cancelled external action.
- CLI and SPA use REST/SSE only; they must not read the service's SQLite database or call providers directly.

## OpenAPI, kernel, and CLI

- The approved `opal-api` OpenAPI 3.1 specification and shared kernel define public behavior. Local packaged copies are implementation artifacts; keep them aligned through an **explicitly requested** contract-update process.
- Preserve the suite's standard error envelope, status codes, authentication, correlation headers, identifiers, ETag/precondition behavior, and configuration semantics.
- Use `X-Opal-Session-Id`, `X-Opal-Transaction-Id`, `X-Opal-Request-Id`, and `X-Opal-Causation-Id` according to kernel semantics. They trace work; none is proof of authorization. Generate a fresh request ID per downstream HTTP hop.
- Prefer shared kernel operations over reimplementing an incompatible THINK-specific variant.
- Derive new canonical CLI hierarchy from OpenAPI resource paths and semantic actions from `operationId`. Derive input flags, types, validation, and help from standard parameters and schemas.
- Do not introduce or expand `x-opal-cli`, per-operation CLI mapping extensions, or a second manually maintained CLI contract. Existing CLI code can remain until a separately approved fidelity-tested migration replaces it.
- Preserve documented CLI names and aliases unless clearly inconsistent with approved universal conventions. Report breaking changes before making them.
- CLI-only conveniences (stdin, file output, watching, human presentation) belong in reusable client code, not vendor extensions in the REST contract.
- Never silently add, remove, or rename REST operations to make CLI generation easier.

## Profiles, personas, configuration, and secrets

- Follow OPAL's shared profile and persona identities. THINK owns only its own module-specific profile compartment and reasoning bindings; preserve unrelated compartments on import/export/update.
- Explicit request settings take precedence over persona bindings, selected/active profile defaults, and service defaults according to the contract. Make the effective choice inspectable when appropriate.
- A persona may specify model, instructions, generation behavior, or an optional cross-service voice identity. THINK must not absorb SAY's voice implementation or make SAY a task dependency.
- Keep configuration operations consistent with kernel `save`, `load`, `validate`, and registered-setting behavior. Distinguish validation from mutation; ensure configuration replacement is atomic when promised.
- Enforce strong ETags and `If-Match` on applicable mutations; do not silently overwrite concurrent changes.
- Secrets are environment/secret references, not persisted values in profiles, snapshots, tasks, source control, logs, or CLI examples.
- Environment overrides must follow the documented precedence and must not silently rewrite persisted settings.

## Reliability, security, and performance

- Default to safe local bindings. Non-loopback exposure requires authentication and appropriate transport protection.
- Treat task text, event context, model outputs, action descriptions, retrieved data, and tool results as untrusted. Validate at boundaries and avoid prompt-injection-driven privilege escalation.
- Bound input sizes, histories, stored artifacts, action results, log data, queue depths, and concurrent work. Reject overload explicitly rather than silently dropping admitted tasks.
- Use typed, actionable errors with no secret leakage. Propagate useful context while preserving the contract's error structure.
- Prefer explicit start/stop lifecycle, cleanup on cancellation/shutdown, and deterministic recovery over ad hoc background workers.
- Separate optional operational telemetry from user-facing task output. Do not store excessive transcripts, private reasoning, or unrestricted action payloads.
- Favor predictable behavior on Raspberry Pi-class hardware and standard developer machines; performance improvements must not compromise fidelity.

## Tests and evidence

Test the behavior being changed, including failure cases. At a minimum, choose relevant coverage from:

- Unit tests for provider adapters, model selection, validation, and parsing.
- Contract tests for every affected REST operation, schemas, error/status codes, headers, and preconditions.
- CLI and SPA parity tests proving requests go through REST.
- Task tests for admission, idempotency conflicts, deadlines, cancellation, state transitions, restart recovery, and retention.
- Action-boundary tests proving that unauthorized, malformed, disabled, or unavailable actions **never reach execution**.
- Receipt reconciliation and retry tests proving duplicate side effects are not introduced by ambiguous responses.
- SSE tests for disconnects, backpressure, late subscribers, and terminal outcomes.
- Deterministic evaluation sets for reasoning plus guarded action behavior.

Normal verification commands from the repository root:

```bash
ruff check .
pytest -q
bash scripts/verify.sh
```

Run focused tests during development and the full deterministic suite for substantive changes. Live Ollama, cloud-provider, or external execution tests are **opt-in** and must use clearly identified test endpoints and read-only/safe actions. Never substitute mocked success for evidence of live interoperability.

## Agent workflow and reporting

1. Inspect the affected contract, implementation, and relevant tests before editing.
2. State the smallest change that addresses the requirement. Identify assumptions and inconsistencies directly; do not simply agree with a flawed proposal.
3. Avoid unrelated refactors, opportunistic renaming, new frameworks, speculative features, and dependency proliferation.
4. Keep changes reviewable and reversible. Ask approval before breaking API, CLI, storage, or action-execution compatibility.
5. Never claim a test, benchmark, integration run, or code review occurred unless it actually did.
6. After each implementation step, report:
   - **Completed:** What changed and which files.
   - **Defect risk:** A reasoned probability estimate (%) for a defect introduced by the change; identify uncertainty.
   - **Testing:** Exact commands and actual passed/failed/skipped counts; distinguish mock, contract, and live tests.
   - **Progress:** Step N of M, when working from a numbered plan.
   - **Next:** The next intended step and any unresolved decisions.
7. When presenting choices, compare complexity (1–10), tradeoffs, and a recommendation with confidence. Do not silently make architecture decisions on the owner's behalf.
8. When working under a step-by-step plan, respect the user's requested stop/continue cadence. Do not undertake later steps without authorization if the plan calls for a checkpoint.

**Primary decision rule:** Preserve fidelity and the established module boundaries first; simplify second; expand capability only when justified by an explicit requirement.
