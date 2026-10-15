#!/usr/bin/env python3
"""Run the GUI's offline/mock tests without connecting to hardware."""
import json
import os
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for directory in (ROOT / "src", ROOT / "src/dsw", ROOT / "tests/dsw"):
    sys.path.insert(0, str(directory))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests/dsw"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = ROOT / "outputs/dsw/validation"
    output.mkdir(parents=True, exist_ok=True)
    (output / "test_results.json").write_text(json.dumps({
        "platform": platform.platform(), "python": platform.python_version(),
        "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "passed": result.wasSuccessful(), "hardware_access": False,
    }, indent=2) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
