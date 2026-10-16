"""Fusion bridge: interface only.

For now the user exports measurements from Fusion by hand and saves them as
model/measurements.json in the job folder. This stage reads that file. If it is
missing or malformed, the job goes to needs_input.

TODO(fusion): decide how Fusion's API is invoked. That is an open question, so
this module does not call Fusion.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.stages import StageBlocked, StageContext, StageOutput

EXPORT_FILENAME = "measurements.json"


def export_path(folder: Path) -> Path:
    return folder / "model" / EXPORT_FILENAME


def load_measured(folder: Path) -> dict[str, float]:
    """Read the hand-exported measurements. Raises StageBlocked if unusable."""
    path = export_path(folder)
    if not path.exists():
        raise StageBlocked(
            "No Fusion export at model/measurements.json. Export the measurements "
            "from Fusion, save the file there, then press Resume."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageBlocked(f"model/measurements.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise StageBlocked("model/measurements.json must be a non-empty JSON object")
    measured: dict[str, float] = {}
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StageBlocked(f"model/measurements.json: '{name}' is not a number")
        if not math.isfinite(value):
            raise StageBlocked(f"model/measurements.json: '{name}' is not finite")
        measured[str(name)] = float(value)
    return measured


def run(ctx: StageContext) -> StageOutput:
    load_measured(ctx.folder)  # raises StageBlocked when the export is missing
    return StageOutput(output_path=export_path(ctx.folder))
