"""
Where DriftClean's own artifacts live.

The drift reports the webchat lane writes embed the model's reasoning excerpt
and the task hint verbatim, so they are a source like any other — but unlike
the agent transcripts, their location is a convention rather than a path the
tool can just know. One resolver, so the cleaner and the discovery path can
never disagree about where to look.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

# <checkout>/src/drift_clean/paths.py -> <checkout>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def drift_report_dirs() -> Tuple[Path, ...]:
    """Every place a drift report is looked for, most specific first."""
    configured = os.environ.get("DRIFT_REPORT_DIR", "").strip()
    candidates = (
        Path(configured).expanduser() if configured else None,
        # The documented convention: a workspace-level audits directory
        # alongside the checkout.
        PROJECT_ROOT.parent / "audits_plans" / "drift_reports",
        # Or simply keep them with the project.
        PROJECT_ROOT / "drift_reports",
    )
    return tuple(path for path in candidates if path is not None)
