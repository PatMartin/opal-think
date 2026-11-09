"""Vendored contract sanity: the packaged spec is the authoritative 1.6.0 contract."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "openapi"

# vendored name -> authoritative source file under openapi/
VENDORED = {
    "opal_think.yaml": "opal_think_v1.6.0.yaml",
    "shared/opal_kernel.yaml": "shared/opal_kernel.yaml",
}


def _vendored(name: str) -> str:
    return resources.files("opal_think").joinpath(f"contract/{name}").read_text(encoding="utf-8")


def test_vendored_contract_is_1_6_0() -> None:
    spec = yaml.safe_load(_vendored("opal_think.yaml"))
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["version"] == "1.6.0"
    assert "/api/v1/task/" in spec["paths"]
    assert "/api/v1/health/ready" in spec["paths"]


def test_vendored_contract_identical_to_openapi_source() -> None:
    for vendored_name, source_rel in VENDORED.items():
        assert _vendored(vendored_name) == (CONTRACT_DIR / source_rel).read_text(encoding="utf-8")


def test_kernel_pathitem_refs_resolve() -> None:
    spec = yaml.safe_load(_vendored("opal_think.yaml"))
    kernel = yaml.safe_load(_vendored("shared/opal_kernel.yaml"))
    path_items = kernel["components"]["pathItems"]
    for path, item in spec["paths"].items():
        if "$ref" in item:
            ref = item["$ref"]
            assert ref.startswith("./shared/opal_kernel.yaml#/components/pathItems/"), f"{path}: {ref}"
            assert ref.rsplit("/", 1)[1] in path_items, f"{path}: {ref}"
