"""Checks: pure Python comparison of measured values against expectations.

Test fixture: the bracket. Its expectations are below. Typed measurements from
the job entry form override the fixture value with the same name, because the
operator entered them for this job.

Axis mapping (assumed, confirm with the user): length = X = 120 mm,
width = Y = 40 mm, height = Z = 45 mm.

TODO(checks): per-part expectations. Volume and mass only make sense for the
bracket, so other parts will need their own expectation sets.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from app import db
from app.jobs import next_free_path, write_new
from app.stages import StageContext, StageOutput
from app.stages.fusion_bridge import load_measured

EPS = 1e-9
BRACKET_DENSITY_G_PER_CM3 = 2.70  # aluminium 6061; documents the mass expectation


@dataclass(frozen=True)
class Expectation:
    name: str
    expected: float
    tolerance: float  # absolute, in the same unit as expected
    unit: str


@dataclass(frozen=True)
class CheckResult:
    check_name: str
    passed: bool
    measured: float | None
    expected: float
    tolerance: float
    detail: str


def _percent(expected: float, pct: float) -> float:
    return abs(expected) * pct / 100.0


BRACKET_FIXTURE: tuple[Expectation, ...] = (
    Expectation("length", 120.0, 0.05, "mm"),
    Expectation("width", 40.0, 0.05, "mm"),
    Expectation("height", 45.0, 0.05, "mm"),
    Expectation("hole_count", 4.0, 0.0, "count"),
    Expectation("hole_diameter", 5.5, 0.05, "mm"),
    Expectation("volume", 31600.0, _percent(31600.0, 1.0), "mm^3"),
    # About 85 g at 2.70 g/cm^3 (31.6 cm^3 x 2.70 is about 85.3 g; the spec says 85).
    Expectation("mass", 85.0, _percent(85.0, 1.0), "g"),
)


def build_expectations(typed_rows) -> list[Expectation]:
    """Fixture expectations, with typed job measurements overriding by name."""
    typed = {
        r["name"]: Expectation(r["name"], float(r["expected"]),
                               float(r["tolerance"] or 0.0), r["unit"])
        for r in typed_rows
    }
    merged = [typed.pop(e.name, e) for e in BRACKET_FIXTURE]
    merged.extend(typed.values())
    return merged


def run_checks(measured: dict[str, float], expectations) -> list[CheckResult]:
    results: list[CheckResult] = []
    for exp in expectations:
        value = measured.get(exp.name)
        if value is None or not math.isfinite(value):
            results.append(CheckResult(
                check_name=exp.name,
                passed=False,
                measured=None,
                expected=exp.expected,
                tolerance=exp.tolerance,
                detail=f"No measured value for {exp.name} in the Fusion export.",
            ))
            continue
        diff = abs(value - exp.expected)
        passed = diff <= exp.tolerance + EPS
        if passed:
            verdict = "within tolerance"
        else:
            verdict = f"off by {diff:g} {exp.unit}, outside tolerance"
        results.append(CheckResult(
            check_name=exp.name,
            passed=passed,
            measured=value,
            expected=exp.expected,
            tolerance=exp.tolerance,
            detail=(
                f"{exp.name}: measured {value:g} {exp.unit}, expected "
                f"{exp.expected:g} +/- {exp.tolerance:g} {exp.unit}; {verdict}."
            ),
        ))
    return results


def run(ctx: StageContext) -> StageOutput:
    measured = load_measured(ctx.folder)
    typed = db.get_measurements(ctx.conn, ctx.job_id, source="typed")
    results = run_checks(measured, build_expectations(typed))

    created_at = db.now_iso()
    db.save_checks(ctx.conn, ctx.job_id, results, created_at)

    out_path = next_free_path(ctx.folder / "checks", "run", ".json")
    payload = {
        "job_id": ctx.job_id,
        "created_at": created_at,
        "results": [asdict(r) for r in results],
    }
    write_new(out_path, json.dumps(payload, indent=2) + "\n")
    return StageOutput(
        output_path=out_path,
        note=f"{sum(r.passed for r in results)} of {len(results)} checks passed",
    )
