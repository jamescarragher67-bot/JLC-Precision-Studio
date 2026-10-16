"""Shared test data. Plain constants and helpers, no fixtures."""

from __future__ import annotations

import json

from app.jobs import write_new

BRACKET_ARGS = dict(
    client="Acme Fixtures",
    order_text="Mounting bracket, 120 x 40 x 45 mm, four 5.5 mm holes.",
    length_mm=120.0,
    width_mm=40.0,
    hole_diameter_mm=5.5,
    price=45.0,
    deadline="2026-11-01",
)

# A Fusion export that passes every bracket check.
GOOD_MEASUREMENTS = {
    "length": 120.02,
    "width": 39.97,
    "height": 45.01,
    "hole_count": 4,
    "hole_diameter": 5.52,
    "volume": 31650.0,
    "mass": 85.4,
}


def write_export(jobs_root, job_id: str, values: dict | None = None):
    """Simulate the user saving a Fusion export into the job's model/ folder."""
    model = jobs_root / job_id / "model"
    model.mkdir(exist_ok=True)
    return write_new(model / "measurements.json", json.dumps(values or GOOD_MEASUREMENTS))
