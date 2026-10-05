"""Keep installed settings and credentials outside the development folder."""

import os
from pathlib import Path

INSTALLED_RUNTIME = Path.home() / "Library" / "Application Support" / "TickTick Display"


def runtime_root():
    configured = os.environ.get("TICKTICK_DISPLAY_RUNTIME")
    if configured:
        return Path(configured)
    if (INSTALLED_RUNTIME / ".installed").exists():
        return INSTALLED_RUNTIME
    return Path(__file__).parent
