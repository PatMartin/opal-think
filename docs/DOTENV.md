# Dotenv configuration

OPAL Think uses `python-dotenv` for local environment defaults.

## Precedence

Configuration is resolved in this order:

1. Explicit CLI arguments
2. Existing environment variables (including empty strings)
3. `.env` entries
4. Configuration-file values
5. Built-in defaults

Dotenv loading is called with `override=False`; a variable already present in
the process environment is never replaced by `.env`, even when it is empty.

## Startup behavior

Executable entry points resolve exactly `./.env` relative to the current
working directory. Parent directories are not searched. Python package and
application-factory imports do not load, create, or mutate dotenv state.

If the default `.env` is missing, the entry point creates a local snapshot
from supported configuration-file values and built-in defaults before
evironment and CLI overrides are applied. Creation uses an exclusive create
operation and restrictive permissions where the platform supports them.
Secrets are emitted only as commented placeholders. If creation fails, startup
continues with the existing sources and emits a warning.

The generated file is a snapshot, not a synchronized view: active entries
continue to take precedence over later configuration-file changes until the
entry is edited or removed. Existing `.env` files are never overwritten or
regenerated.

## Controls

```bash
# use ./.env (created when missing)
opal-think-svc

# load an explicit file; relative paths resolve from the current directory
opal-think-svc --env-file config/local.env

# disable both loading and automatic creation
opal-think-svc --no-env-file
```

`--env-file` and `--no-env-file` are mutually exclusive. An explicitly named
missing or unreadable file is an error and is never created implicitly.

## OPAL-managed services

Standalone service entry points own loading and initialization. Embedded
application factories accept already-resolved configuration and do not load
`.env` themselves. When the `opal` orchestrator launches services as
subprocesses, it propagates `OPAL_DOTENV_FILE` (the resolved orchestrator
file) or `OPAL_NO_DOTENV=1`; service-specific environment settings cannot
redirect the file or re-enable a disabled policy. The orchestrator then adds
its documented service host/port variables so they still override configured
values.

See `.env.example` for supported variables. Variables containing tokens,
passwords, credentials, or API keys are intentionally omitted from generated
snapshots.
