"""Run every evalset YAML case through the deterministic harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.runner import assert_case, load_cases, run_case

EVALSET_DIR = Path(__file__).resolve().parents[2] / "evalsets"
CASES = [
    pytest.param(path, name, case, id=f"{path.stem}::{case['id']}")
    for path in sorted(EVALSET_DIR.glob("*.yaml"))
    for name, cases in [load_cases(path)]
    for case in cases
]


@pytest.mark.parametrize("evalset_path, evalset_name, case", CASES)
def test_evalset_case(evalset_path, evalset_name, case, tmp_path) -> None:
    task, event_calls = run_case(evalset_name, case, tmp_path)
    assert_case(case, task, event_calls)
