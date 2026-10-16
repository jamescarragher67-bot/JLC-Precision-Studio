"""Checks: the bracket fixture passes and fails at the right tolerances."""

import pytest

from app.stages.checks import BRACKET_FIXTURE, build_expectations, run_checks
from tests.helpers import GOOD_MEASUREMENTS


def _by_name(results):
    return {r.check_name: r for r in results}


def test_bracket_fixture_passes_with_a_good_export():
    results = run_checks(GOOD_MEASUREMENTS, BRACKET_FIXTURE)
    assert len(results) == len(BRACKET_FIXTURE) == 7
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]
    assert all(r.detail for r in results)


def test_fixture_values_match_the_spec():
    exp = {e.name: e for e in BRACKET_FIXTURE}
    assert (exp["length"].expected, exp["length"].tolerance) == (120.0, 0.05)
    assert (exp["width"].expected, exp["width"].tolerance) == (40.0, 0.05)
    assert (exp["height"].expected, exp["height"].tolerance) == (45.0, 0.05)
    assert (exp["hole_count"].expected, exp["hole_count"].tolerance) == (4.0, 0.0)
    assert (exp["hole_diameter"].expected, exp["hole_diameter"].tolerance) == (5.5, 0.05)
    assert exp["volume"].expected == 31600.0
    assert exp["volume"].tolerance == pytest.approx(316.0)  # 1 percent
    assert exp["mass"].expected == 85.0
    assert exp["mass"].tolerance == pytest.approx(0.85)  # 1 percent


def test_axis_just_outside_tolerance_fails():
    measured = {**GOOD_MEASUREMENTS, "length": 120.06}
    result = _by_name(run_checks(measured, BRACKET_FIXTURE))["length"]
    assert not result.passed
    assert result.measured == 120.06
    assert "outside tolerance" in result.detail


def test_value_exactly_on_the_tolerance_edge_passes():
    measured = {**GOOD_MEASUREMENTS, "length": 120.05}
    assert _by_name(run_checks(measured, BRACKET_FIXTURE))["length"].passed


def test_hole_count_must_be_exactly_four():
    measured = {**GOOD_MEASUREMENTS, "hole_count": 5}
    assert not _by_name(run_checks(measured, BRACKET_FIXTURE))["hole_count"].passed


def test_volume_and_mass_use_one_percent():
    just_inside = {**GOOD_MEASUREMENTS, "volume": 31600 * 1.0099, "mass": 85 * 1.0099}
    just_outside = {**GOOD_MEASUREMENTS, "volume": 31600 * 1.011, "mass": 85 * 1.011}
    inside = _by_name(run_checks(just_inside, BRACKET_FIXTURE))
    outside = _by_name(run_checks(just_outside, BRACKET_FIXTURE))
    assert inside["volume"].passed and inside["mass"].passed
    assert not outside["volume"].passed and not outside["mass"].passed


def test_missing_measurement_fails_with_a_clear_detail():
    measured = {k: v for k, v in GOOD_MEASUREMENTS.items() if k != "mass"}
    result = _by_name(run_checks(measured, BRACKET_FIXTURE))["mass"]
    assert not result.passed
    assert result.measured is None
    assert "No measured value for mass" in result.detail


def test_typed_measurements_override_the_fixture_by_name():
    typed = [{"name": "length", "expected": 121.0, "tolerance": 0.05, "unit": "mm"}]
    expectations = {e.name: e for e in build_expectations(typed)}
    assert expectations["length"].expected == 121.0
    # Fixture values not typed by the operator are kept.
    assert expectations["width"].expected == 40.0
    result = _by_name(run_checks(GOOD_MEASUREMENTS, build_expectations(typed)))["length"]
    assert not result.passed  # 120.02 against the typed 121.0
