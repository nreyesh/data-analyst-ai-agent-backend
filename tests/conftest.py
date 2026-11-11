"""Pytest configuration and environment bootstrapping."""

import sys
import types
from pathlib import Path

# Ensure 'backend' namespace is importable regardless of whether repository is
# cloned as 'backend/' (monorepo) or 'data-analyst-ai-agent-backend/' (CI runner).
if "backend" not in sys.modules:
    pkg = types.ModuleType("backend")
    pkg.__path__ = [str(Path(__file__).resolve().parent.parent)]
    sys.modules["backend"] = pkg
