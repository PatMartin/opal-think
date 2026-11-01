# OPAL-THINK

Model-neutral, guarded reasoning service. The OpenAPI contract
(`opal_think.v1.4.0.yaml`, vendored at `src/opal_think/contract/opal_think.yaml`)
is authoritative; the Typer CLI and SPA call the REST API only.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
pytest
opal-think-svc              # serves http://127.0.0.1:8080
opal think health
```
