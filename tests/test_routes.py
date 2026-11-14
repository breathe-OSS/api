import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api import routes
from app.core import database
from app.core.config import ESTIMATE_ZONES, PLACES, SENSOR_ZONES, ZONES
from app.core.conversions import get_us_aqi

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize(
    "token, seconds",
    [
        ("15m", 900),
        ("1h", 3600),
        ("6H", 6 * 3600),
        ("1d", 86400),
        ("2w", 14 * 86400),
        ("1mo", 30 * 86400),
        ("3mo", 90 * 86400),
        ("1y", 365 * 86400),
        ("whatever", routes.DEFAULT_RANGE_SECONDS),
    ],
)
def test_parse_time(token, seconds):
    assert routes.parse_time(token) == seconds


def _ist(*args):
    return int(datetime(*args, tzinfo=IST).timestamp())


@pytest.mark.parametrize(
    "token, start, end",
    [
        ("2026-10", _ist(2026, 10, 1), _ist(2026, 11, 1)),
        ("2026-12", _ist(2026, 12, 1), _ist(2027, 1, 1)),
        ("2026-W42", _ist(2026, 10, 12), _ist(2026, 10, 19)),
        ("2026-w42", _ist(2026, 10, 12), _ist(2026, 10, 19)),
        ("2026-10-15", _ist(2026, 10, 15), _ist(2026, 10, 16)),
        ("2026-10-01_2026-10-07", _ist(2026, 10, 1), _ist(2026, 10, 8)),
        ("2024-02-29", _ist(2024, 2, 29), _ist(2024, 3, 1)),
    ],
)
def test_parse_window(token, start, end):
    assert routes.parse_window(token) == (start, end)


def test_parse_window_rolling_is_none():
    assert routes.parse_window("1mo") is None


@pytest.mark.parametrize(
    "token",
    ["2026-13", "2026-W60", "2026-10-07_2026-10-01", "2025-02-29", "2026-10-1", "1999-01", "nonsense-x"],
)
def test_parse_window_rejects_bad_tokens(token):
    with pytest.raises(HTTPException) as err:
        routes.parse_window(token)

    assert err.value.status_code == 400


def test_page_walks_with_cursor():
    items = [{"ts": ts} for ts in (10, 20, 30, 40, 50)]

    first, cursor = routes._page(items, 2, None)
    second, cursor2 = routes._page(items, 2, cursor)
    last, cursor3 = routes._page(items, 2, cursor2)

    assert [i["ts"] for i in first] == [10, 20]
    assert cursor == 20
    assert [i["ts"] for i in second] == [30, 40]
    assert [i["ts"] for i in last] == [50]
    assert cursor3 is None


def test_page_exact_fit_has_no_next_cursor():
    page, cursor = routes._page([{"ts": 1}, {"ts": 2}], 2, None)

    assert len(page) == 2
    assert cursor is None


def test_stats_skip_missing_values():
    tallies = routes._new_tallies(["pm2_5", "pm10"])
    routes._tally(tallies, {"pm2_5": 10.0})
    routes._tally(tallies, {"pm2_5": 20.0, "pm10": None})

    stats = routes._stats(tallies)

    assert stats["max_pm2_5"] == 20.0
    assert stats["min_pm2_5"] == 10.0
    assert stats["avg_pm2_5"] == 15.0
    assert stats["avg_pm10"] is None
    assert routes._stats(routes._new_tallies(["pm2_5"])) == {}


def test_list_zones_default(client):
    body = client.get("/zones").json()

    assert {z["id"] for z in body["zones"]} == set(ZONES)
    assert all({"lat", "lon", "zone_type", "provider"} <= set(z) for z in body["zones"])


def test_list_zones_sensor_layout(client):
    body = client.get("/zones", params={"layout": "sensors"}).json()
    by_id = {z["id"]: z for z in body["zones"]}

    assert set(by_id) == set(PLACES)
    assert by_id["kanipora"]["kind"] == "sensor"
    assert by_id["kanipora"]["legacy_zone"] == "srinagar"
    assert by_id["anantnag_city"]["kind"] == "estimate"
    assert by_id["anantnag_city"]["legacy_zone"] == "anantnag_city"


def test_sensor_ids_do_not_clash_with_zone_ids():
    assert not set(SENSOR_ZONES) & set(ZONES)


def test_sensor_info(client):
    body = client.get("/sensor-info").json()

    assert "sensors" in body


def test_unknown_zone_is_404(client):
    assert client.get("/aqi/zone/atlantis").status_code == 404


def test_zone_route_serves_sensor_and_estimate(client, monkeypatch):
    async def fake_place(place_id, force_refresh=False):
        return {"zone_id": place_id, "via": "place"}

    monkeypatch.setattr(routes, "get_place_data", fake_place)

    assert client.get("/aqi/zone/kanipora").json() == {"zone_id": "kanipora", "via": "place"}
    assert client.get("/aqi/zone/anantnag_city").json()["via"] == "place"


def test_zone_route_serves_legacy_sensor_zone(client, monkeypatch):
    async def fake_zone(zone_id, name, lat, lon, zone_type, force_refresh=False):
        return {"zone_id": zone_id, "zone_type": zone_type}

    monkeypatch.setattr(routes, "get_zone_data", fake_zone)

    assert client.get("/aqi/zone/srinagar").json() == {
        "zone_id": "srinagar",
        "zone_type": ZONES["srinagar"].get("zone_type", "hills"),
    }


def test_bulk_zones_dedupes_and_drops_failures(client, monkeypatch):
    async def fake_place(place_id, force_refresh=False):
        if place_id == "bemina":
            raise RuntimeError("upstream down")
        return {"zone_id": place_id}

    monkeypatch.setattr(routes, "get_place_data", fake_place)

    body = client.get("/aqi/zones", params={"ids": "kanipora, bemina,kanipora,,hazratbal"}).json()

    assert [z["zone_id"] for z in body["zones"]] == ["kanipora", "hazratbal"]


def test_bulk_zones_unknown_id_is_404(client):
    response = client.get("/aqi/zones", params={"ids": "kanipora,atlantis"})

    assert response.status_code == 404
    assert "atlantis" in response.json()["detail"]


def test_bulk_zones_sensor_layout_covers_every_place(client, monkeypatch):
    async def fake_place(place_id, force_refresh=False):
        return {"zone_id": place_id}

    monkeypatch.setattr(routes, "get_place_data", fake_place)

    body = client.get("/aqi/zones", params={"layout": "sensors"}).json()

    assert [z["zone_id"] for z in body["zones"]] == list(PLACES)


def test_health_reports_degraded_without_redis(client):
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "postgres": "ok", "redis": "down"}


def test_health_ok(client, fake_redis):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_seasonal_maps_sensor_to_its_zone_and_caches(client, fake_redis, monkeypatch):
    calls = []

    async def fake_seasonal(zone_id):
        calls.append(zone_id)
        return {"zone_id": zone_id, "months": []}

    monkeypatch.setattr(routes, "get_zone_seasonal", fake_seasonal)

    first = client.get("/seasonal/gandhi-nagar")
    second = client.get("/seasonal/gandhi-nagar")

    assert first.json() == second.json() == {"zone_id": "jammu_city", "months": []}
    assert calls == ["jammu_city"]
    assert "seasonal:jammu_city" in fake_redis.store


def test_seasonal_unknown_zone(client):
    assert client.get("/seasonal/atlantis").status_code == 404


def test_weather_history_unknown_zone(client):
    assert client.get("/weather-history/atlantis/1d/1h").status_code == 404


def test_weather_history_bad_window(client):
    assert client.get("/weather-history/jammu_city/2026-13/1h").status_code == 400


def test_weather_history_pages(client, monkeypatch):
    seen = {}

    async def fake_weather(lat, lon, range_sec, interval_sec, window):
        seen.update(lat=lat, range_sec=range_sec, interval_sec=interval_sec, window=window)
        return {"points": [{"ts": ts} for ts in (1, 2, 3)]}

    monkeypatch.setattr(routes, "fetch_weather_history", fake_weather)

    body = client.get("/weather-history/kanipora/1w/1h", params={"limit": 2}).json()

    assert body["zone_id"] == "kanipora"
    assert [p["ts"] for p in body["points"]] == [1, 2]
    assert body["next_cursor"] == 2
    assert seen == {
        "lat": SENSOR_ZONES["kanipora"]["lat"],
        "range_sec": 7 * 86400,
        "interval_sec": 3600,
        "window": None,
    }


def test_weather_history_calendar_window(client, monkeypatch):
    seen = {}

    async def fake_weather(lat, lon, range_sec, interval_sec, window):
        seen.update(range_sec=range_sec, window=window)
        return {"points": []}

    monkeypatch.setattr(routes, "fetch_weather_history", fake_weather)

    client.get("/weather-history/jammu_city/2026-10-15/1h")

    assert seen == {"range_sec": 0, "window": (_ist(2026, 10, 15), _ist(2026, 10, 16))}


def _seed(zone_id, values, step=900):
    base = int(time.time() // 3600) * 3600 - 4 * 3600
    readings = [
        {"zone_id": zone_id, "timestamp": base + i * step, "pm2_5": pm25, "pm10": pm10}
        for i, (pm25, pm10) in enumerate(values)
    ]
    database.save_readings(readings)
    database.refresh_15m_rollups()
    return base


def test_historical_json_for_sensor_slug(client):
    base = _seed("doda_town", [(10.0, 20.0), (30.0, 40.0)])

    body = client.get("/historical-data/kashtigarh/1d/15m/pm2.5,pm10").json()

    assert body["data"] == [
        {"zone_id": "kashtigarh", "ts": base, "pm2_5": 10.0, "pm10": 20.0},
        {"zone_id": "kashtigarh", "ts": base + 900, "pm2_5": 30.0, "pm10": 40.0},
    ]
    assert body["stats"]["max_pm2_5"] == 30.0
    assert body["stats"]["min_pm10"] == 20.0
    assert body["stats"]["avg_pm2_5"] == 20.0


def test_historical_computed_us_aqi(client):
    _seed("doda_town", [(75.0, 120.0)])

    body = client.get("/historical-data/doda_town/1d/15m/us_aqi").json()
    row = body["data"][0]

    assert row["us_aqi"] == max(get_us_aqi("pm2_5", 75.0), get_us_aqi("pm10", 120.0))
    assert "pm2_5" not in row


def test_historical_unknown_metrics_fall_back(client):
    _seed("doda_town", [(5.0, 6.0)])

    row = client.get("/historical-data/doda_town/1d/15m/bogus").json()["data"][0]

    assert row["pm2_5"] == 5.0
    assert row["pm10"] == 6.0


def test_historical_empty(client):
    body = client.get("/historical-data/doda_town/1d/1h/pm2.5").json()

    assert body == {"data": [], "stats": {}}


def test_historical_json_paging(client):
    base = _seed("doda_town", [(float(i), 1.0) for i in range(5)])

    first = client.get("/historical-data/doda_town/1d/15m/pm2.5", params={"limit": 2}).json()
    rest = client.get(
        "/historical-data/doda_town/1d/15m/pm2.5",
        params={"limit": 10, "cursor": first["next_cursor"]},
    ).json()

    assert [r["ts"] for r in first["data"]] == [base, base + 900]
    assert first["next_cursor"] == base + 900
    assert first["stats"]["max_pm2_5"] == 1.0
    assert [r["pm2_5"] for r in rest["data"]] == [2.0, 3.0, 4.0]
    assert rest["next_cursor"] is None


def test_historical_limit_out_of_range(client):
    assert client.get("/historical-data/doda_town/1d/15m/pm2.5", params={"limit": 0}).status_code == 422
    assert client.get(
        "/historical-data/doda_town/1d/15m/pm2.5", params={"limit": routes.PAGE_LIMIT_MAX + 1}
    ).status_code == 422


def test_historical_csv_quotes_zone_ids_with_commas(client):
    base = _seed("srinagar_Jeelanabad, Batpora", [(12.5, 30.0)])

    response = client.get("/historical-data/all/1d/15m/pm2.5", params={"format": "csv"})
    rows = list(csv.reader(io.StringIO(response.text)))

    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="historical_all.csv"'
    assert rows[0] == ["zone_id", "ts", "pm2_5"]
    assert rows[1] == ["srinagar_Jeelanabad, Batpora", str(base), "12.5"]


def test_historical_csv_paging_header(client):
    base = _seed("doda_town", [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)])

    response = client.get(
        "/historical-data/doda_town/1d/15m/pm2.5", params={"format": "csv", "limit": 2}
    )

    assert response.headers["x-next-cursor"] == str(base + 900)
    assert len(response.text.strip().splitlines()) == 3


def test_historical_download_name_is_sanitised(client):
    response = client.get('/historical-data/a"b c/1d/15m/pm2.5', params={"format": "csv"})

    assert response.headers["content-disposition"] == 'attachment; filename="historical_a_b_c.csv"'


def test_historical_bad_window(client):
    assert client.get("/historical-data/doda_town/2026-W99/1h/pm2.5").status_code == 400


def test_historical_calendar_window(client):
    day = datetime.now(IST).date() - timedelta(days=2)
    start = _ist(day.year, day.month, day.day)
    database.save_readings([
        {"zone_id": "doda_town", "timestamp": start + 600, "pm2_5": 8.0, "pm10": 1.0},
        {"zone_id": "doda_town", "timestamp": start + 86400 + 600, "pm2_5": 99.0, "pm10": 1.0},
    ])
    database.refresh_15m_rollups()

    body = client.get(f"/historical-data/doda_town/{day.isoformat()}/1d/pm2.5").json()

    assert body["data"] == [{"zone_id": "doda_town", "ts": start, "pm2_5": 8.0}]


def test_historical_is_served_from_cache(client, fake_redis):
    _seed("doda_town", [(10.0, 20.0)])
    url = "/historical-data/doda_town/1d/15m/pm2.5"

    first = client.get(url).json()
    assert "hist:doda_town:1d:15m:pm2.5:json" in fake_redis.store

    conn = database.get_connection()
    conn.execute("DELETE FROM sensor_readings_15m")
    conn.commit()
    conn.close()

    assert client.get(url).json() == first
    assert json.loads(fake_redis.store["hist:doda_town:1d:15m:pm2.5:json"]) == first


def test_estimate_zones_are_not_sensors():
    assert not set(ESTIMATE_ZONES) & set(SENSOR_ZONES)
    assert all(ZONES[z]["provider"] != "airgradient" for z in ESTIMATE_ZONES)
