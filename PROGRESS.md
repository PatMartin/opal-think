# OPAL-THINK Remediation Progress

Evidence log per the remediation pack's reporting standard. Counts are actual;
unavailable figures are marked NOT RUN/UNKNOWN.

| Step | Repo | SHA | Branch | Date | Tests (cmd → passed/failed/skipped) | Lint | Notes |
|---|---|---|---|---|---|---|---|
| 01 | opal-think | (pending) | main | 2026-11-04 | `pytest -q` → 154/0/3 (env-gated live); `pytest tests/contract/test_openapi_surface.py` → 5/0/0 | ruff PASS | CI added (lint + test 3.11/3.12 + opt-in live job); generated-vs-contract OpenAPI inventory asserted 19 ops both directions + tamper check; `scripts/verify.sh` → VERIFY PASS. CI itself NOT RUN (no remote run yet) |

## Confidence (subjective, not statistical)

- Step 01: **88%** — no runtime code touched; new tests + CI config only. Uncertainty:
  workflow hasn't executed on GitHub yet (syntax verified structurally, not by a real
  run); `uv pip install --system` semantics on CI are standard but unproven here;
  the 19-op inventory test asserts the generated contract self-consistently but can't
  catch a contract edit that also removes the code.
