# OPAL-THINK project notes

- Contract is authoritative: `openapi/opal_think_v1.6.0.yaml` (+ `openapi/shared/opal_kernel.yaml`;
  vendored byte-identical at `src/opal_think/contract/opal_think.yaml` and `contract/shared/opal_kernel.yaml`).
  Do not silently change public API behavior.
- Stack: FastAPI + Pydantic v2 + Typer + HTTPX, asyncio, SQLite, src layout. Mirrors `../opal-event` conventions.
- Install: `uv venv && uv pip install -e ".[dev]"`
- Test: `pytest` (pytest-asyncio auto). Lint: `ruff check .` (line-length 130, py311).
- Serve: `opal-think-svc` or `python -m opal_think` (binds 127.0.0.1:8080 by default; non-loopback requires REST auth).
- CLI: `opal think ...` (shared `opal` root aggregates sibling apps) and `opal-think ...` standalone.
- Conventions: OpalError envelope on every error incl. validation; OPAL X-Opal-* headers echoed on every response;
  strong ETags + mandatory If-Match on settings mutations; SSE task streams; no task deletion.
