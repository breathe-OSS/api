import time

import pytest

from app.core import database
from app.core.config import ESTIMATE_ZONES
from app.services import forecast

NOW = 1_800_000_000.0
LATEST = (int(NOW // 900) - 1) * 900


def _rows(values):
    return [{"ts": ts, "pm2_5": pm2_5, "pm10": pm10} for ts, pm2_5, pm10 in values]


def _point(payload, horizon):
    return next(p for p in payload["points"] if p["horizon_minutes"] == horizon)


def test_flat_history_forecasts_flat():
    rows = _rows((LATEST - i * 900, 40.0, 80.0) for i in range(100))

    payload = forecast.forecast_from_history("jammu_city", rows, now=NOW)

    assert payload["issued_at"] == LATEST
    assert [p["horizon_minutes"] for p in payload["points"]] == forecast.HORIZONS_MINUTES
    for point in payload["points"]:
        assert point["pm2_5"] == 40.0
        assert point["pm10"] == 80.0
        assert point["uses_day_before"] is True


def test_blends_latest_with_day_before_target():
    day_before_6h = LATEST + 6 * 3600 - 86400
    rows = _rows([
        (day_before_6h, 20.0, 50.0),
        (LATEST, 100.0, 150.0),
    ])

    point = _point(forecast.forecast_from_history("jammu_city", rows, now=NOW), 360)

    assert point["pm2_5"] == pytest.approx(0.45 * 100.0 + 0.55 * 20.0)
    assert point["pm10"] == pytest.approx(0.45 * 150.0 + 0.55 * 50.0)
    assert point["ts"] == LATEST + 6 * 3600


def test_srinagar_uses_its_own_weights():
    day_before_12h = LATEST + 12 * 3600 - 86400
    rows = _rows([
        (day_before_12h, 10.0, 10.0),
        (LATEST, 110.0, 110.0),
    ])

    point = _point(forecast.forecast_from_history("srinagar", rows, now=NOW), 720)

    assert point["pm2_5"] == pytest.approx(0.30 * 110.0 + 0.70 * 10.0)


def test_readings_inside_bucket_are_averaged():
    rows = _rows([
        (LATEST, 10.0, 20.0),
        (LATEST + 300, 30.0, 40.0),
    ])

    payload = forecast.forecast_from_history("jammu_city", rows, now=NOW)

    assert payload["current"] == {"pm2_5": 20.0, "pm10": 30.0}


def test_missing_day_before_falls_back_to_latest():
    rows = _rows([(LATEST, 60.0, 90.0)])

    point = _point(forecast.forecast_from_history("jammu_city", rows, now=NOW), 180)

    assert point["pm2_5"] == 60.0
    assert point["uses_day_before"] is False


def test_never_reads_past_latest_bucket():
    for horizon in forecast.HORIZONS_MINUTES:
        assert horizon // 15 - forecast.DAY_STEPS <= 0


def test_stale_history_gives_nothing():
    rows = _rows([(NOW - 2 * 3600, 60.0, 90.0)])

    assert forecast.forecast_from_history("jammu_city", rows, now=NOW) is None
    assert forecast.forecast_from_history("jammu_city", [], now=NOW) is None


def _seed_zone(zone_id, pm2_5, pm10):
    now = time.time()
    database.save_readings([
        {
            "zone_id": zone_id,
            "pm2_5": pm2_5,
            "pm10": pm10,
            "temp": None,
            "humidity": None,
            "timestamp": now - i * 900,
        }
        for i in range(4)
    ])


def test_route_maps_sensor_to_city_and_caches(client, fake_redis):
    _seed_zone("jammu_city", 35.0, 70.0)

    response = client.get("/forecast/gandhi-nagar")
    body = response.json()

    assert response.status_code == 200
    assert body["zone_id"] == "jammu_city"
    assert body["experimental"] is True
    assert _point(body, 60)["pm2_5"] == 35.0
    assert "forecast:jammu_city" in fake_redis.store

    again = client.get("/forecast/gandhi-nagar")
    assert again.json() == body
    assert again.headers["cache-control"] == "public, max-age=300"


def test_route_rejects_other_zones(client):
    estimate = next(iter(ESTIMATE_ZONES))

    assert client.get("/forecast/atlantis").status_code == 404
    assert client.get(f"/forecast/{estimate}").status_code == 404
    assert client.get("/forecast/kashtigarh").status_code == 404


def test_route_without_recent_readings_is_503(client):
    assert client.get("/forecast/srinagar").status_code == 503
