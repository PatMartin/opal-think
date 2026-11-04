"""Run every evalset YAML case through the deterministic harness."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.eval.runner import assert_case, generate_report, live_configured, load_cases, run_case

EVALSET_DIR = Path(__file__).resolve().parents[2] / "evalsets"
CASES = [
    pytest.param(path, name, case, id=f"{path.stem}::{case['id']}")
    for path in sorted(EVALSET_DIR.glob("*.yaml"))
    for name, cases in [load_cases(path)]
    for case in cases
]


@pytest.mark.parametrize("evalset_path, evalset_name, case", CASES)
def test_evalset_case(evalset_path, evalset_name, case, tmp_path) -> None:
    if case.get("live") and not live_configured():
        pytest.skip("live model eval requires OPAL_THINK_LIVE_OLLAMA + OPAL_THINK_LIVE_MODEL")
    task, event_calls, outcome = run_case(evalset_name, case, tmp_path)
    assert_case(case, task, event_calls, outcome)


def test_eval_report_is_machine_readable(tmp_path) -> None:
    """The summary artifact has explicit counts and per-case outcomes."""
    report = generate_report(EVALSET_DIR, tmp_path)
    counts = report["counts"]
    assert counts["total"] == len(report["cases"])
    assert counts["passed"] + counts["failed"] + counts["skipped_live"] == counts["total"]
    assert counts["failed"] == 0
    assert counts["skipped_live"] >= 1  # live_ollama.yaml is gated
    assert report["environment"]["git_sha"] != "unknown" or os.environ.get("CI") is None
    assert all("evalset" in c and "case_id" in c and "outcome" in c for c in report["cases"])
    json.dumps(report)  # serializable artifact
