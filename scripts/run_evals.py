"""Run all evalsets and write the machine-readable quality report.

Usage: python scripts/run_evals.py [out.json]

Exit code 0 when every deterministic case passes; 1 on any failure. Live cases
are skipped unless OPAL_THINK_LIVE_OLLAMA + OPAL_THINK_LIVE_MODEL are set, and
are always marked distinctly in the report.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.eval.runner import generate_report  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evalsets" / "report.json"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # sqlite handles linger on Windows
        report = generate_report(ROOT / "evalsets", Path(tmp))
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    c = report["counts"]
    print(
        f"eval report -> {out}\n"
        f"  total={c['total']} passed={c['passed']} failed={c['failed']} skipped_live={c['skipped_live']}"
    )
    return 0 if c["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
