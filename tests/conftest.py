"""Shared fixtures. Every test runs the code; none reads source text."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The tests are of the module on its own unless a test says otherwise.
os.environ.pop("SHACK_PLATFORM", None)
os.environ.setdefault("DIGIMODES_DATA_DIR", tempfile.mkdtemp(prefix="digimodes-test-"))

VECTORS = ROOT / "tests" / "vectors"
