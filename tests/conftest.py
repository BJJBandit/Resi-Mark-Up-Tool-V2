"""Tests run without an API key or any client drawings: Claude is never called."""

import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
