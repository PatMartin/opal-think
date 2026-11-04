#!/usr/bin/env bash
# Local verification: lint + deterministic suite with unambiguous pass/fail counts.
# No live Ollama/OpenAI/EVENT required (integration tests self-skip without env).
set -u

PY="python"
[ -x ".venv/Scripts/python.exe" ] && PY=".venv/Scripts/python.exe"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
RUFF="ruff"
[ -x ".venv/Scripts/ruff.exe" ] && RUFF=".venv/Scripts/ruff.exe"
[ -x ".venv/bin/ruff" ] && RUFF=".venv/bin/ruff"

echo "== ruff =="
"$RUFF" check .
LINT=$?

echo "== pytest (deterministic) =="
"$PY" -m pytest -q 2>&1 | tail -15
TESTS=${PIPESTATUS[0]}

echo "== summary =="
if [ "$LINT" -eq 0 ] && [ "$TESTS" -eq 0 ]; then
  echo "VERIFY PASS"
else
  echo "VERIFY FAIL (lint=$LINT, pytest=$TESTS)"
  exit 1
fi
