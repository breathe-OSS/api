import math
import time

import pytest

from app.core import database
from app.core.calibration import CALIBRATION_VERSION, epa_pm25


def _past_ist_day(days_ago):
    today = int((time.time() + database.IST_OFFSET) // 86400)
    day = today - days_ago
    return day, day * 86400 - database.IST_OFFSET


def _rows(sql, params=()):
    conn = database.get_connection()
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _aligned_now():
    return int(time.time() // 3600) * 3600 - 3600


def test_save_and_read_history():
    now = time.time()
    database.save_readings([
        {"zone_id": "z", "timestamp": now - 7200, "pm2_5": 10.0, "pm10": 20.0, "temp": 5.0},
        {"zone_id": "z", "timestamp": now - 3600, "pm2_5": 12.0, "pm10": 22.0},
        {"zone_id": "z", "timestamp": now - 30 * 3600, "pm2_5": 99.0, "pm10": 99.0},
        {"zone_id": "other", "timestamp": now - 60, "pm2_5": 1.0, "pm10": 1.0},
    ])

    history = database.get_history("z", hours=24)

    assert [row["pm2_5"] for row in history] == [10.0, 12.0]
    assert history[0]["temp"] == 5.0
    assert history[1]["temp"] is None


def test_duplicate_readings_are_ignored():
    database.save_reading("z", 10.0, 20.0, timestamp=1000.0)
    database.save_reading("z", 55.0, 66.0, timestamp=1000.0)

    rows = _rows("SELECT pm2_5 FROM sensor_readings WHERE zone_id = 'z'")

    assert rows == [{"pm2_5": 10.0}]


def test_save_readings_empty_is_a_no_op():
    database.save_readings([])

    assert _rows("SELECT COUNT(*) AS n FROM sensor_readings") == [{"n": 0}]


def test_raw_backfill_fills_only_missing_raw_columns():
    now = time.time()
    database.save_readings([
        {"zone_id": "z", "timestamp": now - 100, "pm2_5": 10.0, "pm10": 20.0, "pm2_5_raw": 30.0},
    ])

    database.save_raw_backfill([
        {"zone_id": "z", "timestamp": now - 100, "pm2_5_raw": 999.0, "humidity_raw": 40.0},
        {"zone_id": "z", "timestamp": now - 200, "pm2_5_raw": 25.0, "humidity_raw": 41.0},
    ])

    raw = database.get_raw_history("z", hours=1)

    assert [(r["pm2_5_raw"], r["humidity_raw"]) for r in raw] == [(25.0, 41.0), (30.0, 40.0)]
    assert [r["pm2_5"] for r in database.get_history("z", hours=1)] == [10.0]


def test_raw_backfill_window():
    assert database.raw_backfill_window("z") is None

    database.save_readings([
        {"zone_id": "z", "timestamp": 1000.0, "pm2_5": 1.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": 2000.0, "pm2_5": 1.0, "pm10": 1.0, "pm2_5_raw": 2.0},
    ])

    assert database.raw_backfill_window("z") == (1000.0, 2000.0)

    database.set_raw_backfill_reached("z", 1500.0)
    database.set_raw_backfill_reached("z", 1200.0)

    assert database.raw_backfill_window("z") == (1000.0, 1200.0)


def _save_day(zone_id, start, ratio, count=10, rh=50.0):
    readings = []
    for i in range(count):
        raw = 20.0 + 5 * i
        readings.append({
            "zone_id": zone_id,
            "timestamp": start + 3600 * (i + 1),
            "pm2_5": round(epa_pm25(raw, rh) * ratio, 2),
            "pm10": 50.0,
            "pm2_5_raw": raw,
            "humidity_raw": rh,
        })
    database.save_readings(readings)
    return readings


def test_recalibrate_replaces_batch_corrected_day_with_epa():
    _, start = _past_ist_day(3)
    saved = _save_day("batch", start, ratio=1.6)

    assert database.recalibrate_stored_pm25() == 1

    rows = _rows(
        "SELECT pm2_5, pm2_5_ag, pm2_5_raw, humidity_raw, calibrated FROM sensor_readings "
        "WHERE zone_id = 'batch' ORDER BY timestamp"
    )

    for row, original in zip(rows, saved):
        assert row["pm2_5_ag"] == original["pm2_5"]
        assert row["pm2_5"] == pytest.approx(round(epa_pm25(row["pm2_5_raw"], row["humidity_raw"]), 2))
        assert row["calibrated"] == CALIBRATION_VERSION


def test_recalibrate_keeps_epa_day():
    _, start = _past_ist_day(3)
    saved = _save_day("epa", start, ratio=1.0)

    database.recalibrate_stored_pm25()

    rows = _rows("SELECT pm2_5, pm2_5_ag FROM sensor_readings WHERE zone_id = 'epa' ORDER BY timestamp")

    assert [row["pm2_5"] for row in rows] == [r["pm2_5"] for r in saved]
    assert [row["pm2_5_ag"] for row in rows] == [r["pm2_5"] for r in saved]


def test_recalibrate_decides_each_day_on_its_own():
    _, older = _past_ist_day(5)
    _, newer = _past_ist_day(4)
    _save_day("mixed", older, ratio=1.0)
    _save_day("mixed", newer, ratio=1.6)

    assert database.recalibrate_stored_pm25() == 2

    ratios = _rows(
        "SELECT timestamp, pm2_5 / pm2_5_ag AS ratio FROM sensor_readings "
        "WHERE zone_id = 'mixed' ORDER BY timestamp"
    )

    assert all(r["ratio"] == pytest.approx(1.0) for r in ratios if r["timestamp"] < newer)
    assert all(r["ratio"] == pytest.approx(1 / 1.6, rel=0.01) for r in ratios if r["timestamp"] >= newer)


def test_recalibrate_is_incremental():
    _, start = _past_ist_day(3)
    _save_day("batch", start, ratio=1.6)

    database.recalibrate_stored_pm25()

    assert database.recalibrate_stored_pm25() == 0


def test_recalibrate_leaves_rows_without_raw_alone():
    database.save_readings([{"zone_id": "plain", "timestamp": time.time(), "pm2_5": 42.0, "pm10": 1.0}])

    database.recalibrate_stored_pm25()

    assert _rows("SELECT pm2_5, pm2_5_ag, calibrated FROM sensor_readings") == [
        {"pm2_5": 42.0, "pm2_5_ag": None, "calibrated": None}
    ]


def test_rollups_average_into_15_minute_buckets():
    base = _aligned_now()
    database.save_readings([
        {"zone_id": "z", "timestamp": base + 60, "pm2_5": 10.0, "pm10": 30.0},
        {"zone_id": "z", "timestamp": base + 600, "pm2_5": 20.0, "pm10": 40.0},
        {"zone_id": "z", "timestamp": base + 960, "pm2_5": 50.0, "pm10": 50.0},
    ])

    database.refresh_15m_rollups()
    database.refresh_15m_rollups()

    rows = _rows("SELECT ts, pm2_5, pm10 FROM sensor_readings_15m WHERE zone_id = 'z' ORDER BY ts")

    assert rows == [
        {"ts": base, "pm2_5": 15.0, "pm10": 35.0},
        {"ts": base + 900, "pm2_5": 50.0, "pm10": 50.0},
    ]


def test_stream_hourly_from_rollups():
    base = _aligned_now() - 7200
    database.save_readings([
        {"zone_id": "z", "timestamp": base + 100, "pm2_5": 10.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": base + 1000, "pm2_5": 20.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": base + 3700, "pm2_5": 33.333, "pm10": 1.0},
    ])
    database.refresh_15m_rollups()

    rows = list(database.stream_historical_data("z", 86400, 3600, ["pm2_5"]))

    assert [(r["ts"], r["pm2_5"]) for r in rows] == [(base, 15.0), (base + 3600, 33.33)]


def test_stream_sub_rollup_interval_reads_raw_table():
    base = _aligned_now()
    database.save_readings([
        {"zone_id": "z", "timestamp": base + 10, "pm2_5": 10.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": base + 310, "pm2_5": 20.0, "pm10": 1.0},
    ])

    rows = list(database.stream_historical_data("z", 86400, 300, ["pm2_5"]))

    assert [(r["ts"], r["pm2_5"]) for r in rows] == [(base, 10.0), (base + 300, 20.0)]


def test_stream_falls_back_to_particulates_for_unknown_metrics():
    base = _aligned_now()
    database.save_readings([{"zone_id": "z", "timestamp": base, "pm2_5": 1.0, "pm10": 2.0, "temp": 3.0}])

    rows = list(database.stream_historical_data("z", 86400, 300, ["bogus"]))

    assert rows[0]["pm2_5"] == 1.0
    assert rows[0]["pm10"] == 2.0
    assert "temp" not in rows[0]


def test_stream_calendar_window_anchors_buckets_at_its_start():
    _, start = _past_ist_day(2)
    database.save_readings([
        {"zone_id": "z", "timestamp": start - 60, "pm2_5": 99.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": start + 60, "pm2_5": 10.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": start + 23 * 3600, "pm2_5": 20.0, "pm10": 1.0},
        {"zone_id": "z", "timestamp": start + 86400, "pm2_5": 99.0, "pm10": 1.0},
    ])
    database.refresh_15m_rollups()

    rows = list(database.stream_historical_data("z", 0, 86400 // 2, ["pm2_5"], (start, start + 86400)))

    assert [(r["ts"], r["pm2_5"]) for r in rows] == [(start, 10.0), (start + 43200, 20.0)]


def test_stream_all_returns_every_zone():
    base = _aligned_now()
    database.save_readings([
        {"zone_id": "a", "timestamp": base, "pm2_5": 1.0, "pm10": 1.0},
        {"zone_id": "b", "timestamp": base, "pm2_5": 2.0, "pm10": 2.0},
    ])

    rows = list(database.stream_historical_data("all", 86400, 300, ["pm2_5"]))

    assert sorted(r["zone_id"] for r in rows) == ["a", "b"]


def test_stream_homogenises_multi_sensor_zone():
    base = _aligned_now() - 3600
    database.save_readings([
        {"zone_id": "srinagar_Kanipora", "timestamp": base, "pm2_5": 40.0, "pm10": 1.0},
        {"zone_id": "srinagar_Bemina", "timestamp": base, "pm2_5": 10.0, "pm10": 1.0},
        {"zone_id": "srinagar", "timestamp": base, "pm2_5": 999.0, "pm10": 1.0},
        {"zone_id": "srinagar", "timestamp": base + 900, "pm2_5": 30.0, "pm10": 1.0},
    ])
    database.refresh_15m_rollups()
    database.save_node_offsets([
        {"zone_id": "srinagar", "node_name": "Kanipora", "metric": "pm2_5", "factor": 2.0, "samples": 1, "updated_at": time.time()},
        {"zone_id": "srinagar", "node_name": "Bemina", "metric": "pm2_5", "factor": 1.0, "samples": 1, "updated_at": time.time()},
        {"zone_id": "srinagar", "node_name": database.OFFSET_LEGACY_KEY, "metric": "pm2_5", "factor": 1.5, "samples": 0, "updated_at": time.time()},
    ])

    rows = list(database.stream_historical_data("srinagar", 86400, 900, ["pm2_5"]))

    assert [(r["zone_id"], r["ts"], r["pm2_5"]) for r in rows] == [
        ("srinagar", base, 15.0),
        ("srinagar", base + 900, 20.0),
    ]


def test_multi_sensor_zone_without_offsets_uses_plain_query():
    base = _aligned_now()
    database.save_readings([{"zone_id": "srinagar", "timestamp": base, "pm2_5": 30.0, "pm10": 1.0}])

    rows = list(database.stream_historical_data("srinagar", 86400, 300, ["pm2_5"]))

    assert [r["pm2_5"] for r in rows] == [30.0]


def test_node_offsets_round_trip_and_reject_non_positive():
    now = time.time()
    database.save_node_offsets([
        {"zone_id": "z", "node_name": "a", "metric": "pm2_5", "factor": 1.2, "samples": 5, "updated_at": now},
        {"zone_id": "z", "node_name": "b", "metric": "pm2_5", "factor": 0.0, "samples": 5, "updated_at": now},
        {"zone_id": "z", "node_name": "a", "metric": "pm10", "factor": -1.0, "samples": 5, "updated_at": now},
    ])
    database.save_node_offsets([
        {"zone_id": "z", "node_name": "a", "metric": "pm2_5", "factor": 0.8, "samples": 6, "updated_at": now},
    ])

    assert database.get_node_offsets("z") == {"pm2_5": {"a": 0.8}}


def _series(stamps, scale):
    return {ts: math.log(scale * (20 + 10 * math.sin(ts / 7))) for ts in stamps}


def test_solve_offsets_recovers_relative_scale():
    stamps = range(0, 200)
    column = {"a": _series(stamps, 1.25), "b": _series(stamps, 0.75)}

    factors = database._solve_offsets(column)

    assert factors["a"] / factors["b"] == pytest.approx(1.25 / 0.75, rel=1e-6)
    assert sum(factors.values()) / 2 == pytest.approx(1.0)


def test_solve_offsets_handles_sensors_with_different_coverage():
    column = {
        "a": _series(range(0, 300), 2.0),
        "b": _series(range(100, 300), 1.0),
        "c": _series(range(0, 150), 0.5),
    }

    factors = database._solve_offsets(column)

    assert factors["a"] / factors["b"] == pytest.approx(2.0, rel=1e-4)
    assert factors["b"] / factors["c"] == pytest.approx(2.0, rel=1e-4)


def test_solve_offsets_needs_two_sensors_with_enough_samples():
    enough = range(database.OFFSET_MIN_SAMPLES)
    short = range(database.OFFSET_MIN_SAMPLES - 1)

    assert database._solve_offsets({"a": _series(enough, 1.0)}) == {}
    assert database._solve_offsets({"a": _series(enough, 1.0), "b": _series(short, 1.0)}) == {}


def test_legacy_factor_uses_sensors_from_the_first_week():
    week = 7 * 86400
    column = {
        "early": {0: 0.0},
        "also_early": {week - 1: 0.0},
        "late": {week + 1: 0.0},
    }
    factors = {"early": 1.2, "also_early": 0.8, "late": 5.0}

    assert database._legacy_factor(column, factors) == pytest.approx(1.0)
    assert database._legacy_factor({}, factors) == 1.0


def test_compute_node_offsets_skips_single_sensor_zone():
    assert database.compute_node_offsets("doda_town") == []
    assert database.compute_node_offsets("not_a_zone") == []


def test_compute_node_offsets_from_rollups():
    start = int(time.time() // 900) * 900 - 900 * 150
    readings = []
    for i in range(120):
        ts = start + i * 900
        level = 20 + 10 * math.sin(i / 5)
        readings.append({"zone_id": "srinagar_Kanipora", "timestamp": ts, "pm2_5": level * 1.5, "pm10": level * 3})
        readings.append({"zone_id": "srinagar_Bemina", "timestamp": ts, "pm2_5": level, "pm10": level * 2})
    database.save_readings(readings)
    database.refresh_15m_rollups()

    rows = database.compute_node_offsets("srinagar")
    by_key = {(r["metric"], r["node_name"]): r["factor"] for r in rows}

    assert by_key[("pm2_5", "Kanipora")] / by_key[("pm2_5", "Bemina")] == pytest.approx(1.5, rel=1e-3)
    assert by_key[("pm10", "Kanipora")] / by_key[("pm10", "Bemina")] == pytest.approx(1.5, rel=1e-3)
    assert ("pm2_5", database.OFFSET_LEGACY_KEY) in by_key


def test_seasonal_climatology_upserts():
    months = [
        {"month": m, "pm2_5": m * 1.0, "pm10": m * 2.0, "precipitation": 0.0, "temp": 10.0, "updated_at": 1.0}
        for m in (2, 1)
    ]
    database.save_seasonal_climatology("z", months)
    database.save_seasonal_climatology("z", [{**months[0], "pm2_5": 77.0}])

    stored = database.get_seasonal_climatology("z")

    assert [row["month"] for row in stored] == [1, 2]
    assert stored[1]["pm2_5"] == 77.0


def test_health_check_on_sqlite():
    assert database.check_postgres_health() is True


def test_bucket_and_table_helpers():
    assert database._pick_table(900) == ("sensor_readings_15m", "ts")
    assert database._pick_table(3600) == ("sensor_readings_15m", "ts")
    assert database._pick_table(300) == ("sensor_readings", "timestamp")
    assert database._pick_table(1000) == ("sensor_readings", "timestamp")
    assert database._resolve_metrics(["pm2.5", "nope", "temp"]) == ["pm2_5", "temp"]
    assert database._resolve_metrics([]) == ["pm2_5", "pm10"]
