"""Vendored contract sanity: the packaged spec is the authoritative 1.4.0 contract."""

from __future__ import annotations

from importlib import resources

import yaml


def test_vendored_contract_matches_repo_contract() -> None:
    vendored = resources.files("opal_think").joinpath("contract/opal_think.yaml").read_text(encoding="utf-8")
    spec = yaml.safe_load(vendored)
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["version"] == "1.4.0"
    assert "/api/v1/tasks/" in spec["paths"]
    assert "/api/v1/health/ready" in spec["paths"]


def test_vendored_contract_identical_to_root_file() -> None:
    from pathlib import Path

    root_spec = Path(__file__).resolve().parents[2] / "opal_think.v1.4.0.yaml"
    vendored = resources.files("opal_think").joinpath("contract/opal_think.yaml").read_text(encoding="utf-8")
    assert vendored == root_spec.read_text(encoding="utf-8")
