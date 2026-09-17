"""Make the repository importable when running from a source checkout.

Keeps ``pytest`` working without an install step, which matters for a project
whose main selling point is "clone it and run one command".
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
