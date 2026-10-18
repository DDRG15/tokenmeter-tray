"""Puts the project root on `sys.path`.

`test_integration_dedupe` imports a helper from `tests.test_proxy`, and that
only works if the root is importable no matter which directory pytest is
launched from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
