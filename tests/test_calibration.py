import sqlite3

import pytest

from app.core.calibration import (
    CALM_LIMIT,
    REGIME_MIN_READINGS,
    calibrate_pm25,
    epa_pm25,
    epa_pm25_sql,
    matches_epa,
    sensor_uses_epa,
)


def test_epa_low_range():
    assert epa_pm25(20, 50) == pytest.approx(0.524 * 20 - 0.0862 * 50 + 5.75)


def test_epa_mid_range():
    assert epa_pm25(100, 60) == pytest.approx(0.786 * 100 - 0.0862 * 60 + 5.75)


def test_epa_high_range_ignores_humidity():
    assert epa_pm25(400, 10) == epa_pm25(400, 90)
    assert epa_pm25(400, 10) == pytest.approx(2.966 + 0.69 * 400 + 8.84e-4 * 400 * 400)


def test_epa_never_negative():
    assert epa_pm25(0, 100) == 0.0


@pytest.mark.parametrize("edge", [30, 50, 210, 260])
def test_epa_is_continuous_at_band_edges(edge):
    below = epa_pm25(edge - 1e-9, 55)
    at = epa_pm25(edge, 55)

    assert below == pytest.approx(at, abs=1e-6)


@pytest.mark.parametrize("pm", [0, 5, 29.9, 30, 42, 50, 120, 209, 210, 235, 259, 260, 450, 900])
@pytest.mark.parametrize("rh", [10, 55, 95])
def test_sql_matches_python(pm, rh):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (pm REAL, rh REAL)")
    conn.execute("INSERT INTO t VALUES (?, ?)", (pm, rh))

    value = conn.execute(f"SELECT {epa_pm25_sql('pm', 'rh', False)} FROM t").fetchone()[0]
    conn.close()

    assert value == pytest.approx(epa_pm25(pm, rh), abs=1e-6)


def test_sql_uses_greatest_on_postgres():
    assert epa_pm25_sql("a", "b", True).startswith("GREATEST(")
    assert epa_pm25_sql("a", "b", False).startswith("MAX(")


def test_matches_epa_tolerance():
    assert matches_epa(10.9, 10.0)
    assert not matches_epa(11.1, 10.0)
    assert matches_epa(102.9, 100.0)
    assert not matches_epa(103.1, 100.0)


def _readings(ratio, count=REGIME_MIN_READINGS, rh=50.0):
    raws = [20.0 + 10 * i for i in range(count)]
    return [(epa_pm25(raw, rh) * ratio, raw, rh) for raw in raws]


def test_sensor_on_epa():
    assert sensor_uses_epa(_readings(1.0)) is True


def test_sensor_on_batch_correction():
    assert sensor_uses_epa(_readings(1.5)) is False


def test_sensor_in_between_is_undecided():
    assert sensor_uses_epa(_readings(1.12)) is None


def test_too_few_readings_is_undecided():
    assert sensor_uses_epa(_readings(1.0, count=REGIME_MIN_READINGS - 1)) is None


def test_spiky_and_incomplete_readings_are_ignored():
    readings = _readings(1.0, count=REGIME_MIN_READINGS - 1)
    readings.append((999.0, CALM_LIMIT + 50, 50.0))
    readings.append((None, 40.0, 50.0))
    readings.append((10.0, None, 50.0))
    readings.append((10.0, 40.0, None))

    assert sensor_uses_epa(readings) is None


def test_median_resists_a_few_mismatched_readings():
    readings = _readings(1.0, count=9)
    readings[0] = (readings[0][0] * 3, readings[0][1], readings[0][2])
    readings[1] = (readings[1][0] * 3, readings[1][1], readings[1][2])

    assert sensor_uses_epa(readings) is True


def test_calibrate_without_raw_keeps_corrected():
    assert calibrate_pm25(12.0, None, 50.0) == 12.0
    assert calibrate_pm25(12.0, 30.0, None) == 12.0


def test_calibrate_without_anything():
    assert calibrate_pm25(None, None, None) is None
    assert calibrate_pm25(None, 30.0, None) == 30.0


def test_calibrate_epa_sensor_keeps_airgradient_value():
    assert calibrate_pm25(77.7, 40.0, 50.0, uses_epa=True) == 77.7


def test_calibrate_batch_sensor_gets_epa():
    assert calibrate_pm25(77.7, 40.0, 50.0, uses_epa=False) == round(epa_pm25(40.0, 50.0), 2)


def test_calibrate_batch_sensor_without_corrected_value():
    assert calibrate_pm25(None, 40.0, 50.0, uses_epa=True) == round(epa_pm25(40.0, 50.0), 2)


def test_calibrate_undecided_checks_the_reading():
    epa = epa_pm25(40.0, 50.0)

    assert calibrate_pm25(epa + 0.5, 40.0, 50.0) == epa + 0.5
    assert calibrate_pm25(epa * 1.5, 40.0, 50.0) == round(epa, 2)
