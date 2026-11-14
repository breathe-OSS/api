import asyncio
import time
from datetime import datetime

import pytest

from app.core import database
from app.core.conversions import calculate_overall_aqi, get_us_aqi
from app.services import fetchers
from app.services.weather import build_weather_text, classify_condition


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def _local(*args):
    return datetime(*args).timestamp()


def _local_hour():
    return int(datetime.now().replace(minute=0, second=0, microsecond=0).timestamp())


@pytest.fixture
def no_festivals(monkeypatch):
    monkeypatch.setattr(fetchers, "_FESTIVAL_DATES", set())


def test_festival_window():
    assert fetchers.is_festival_window(_local(2026, 11, 8, 21))
    assert not fetchers.is_festival_window(_local(2026, 11, 14, 12))


def test_spike_ceilings_rise_on_festival_days():
    assert fetchers._get_spike_ceilings(_local(2026, 11, 8, 21)) == (999, 999)
    assert fetchers._get_spike_ceilings(_local(2026, 7, 1)) == (600, 900)


def test_cold_season_thresholds():
    assert fetchers._get_spike_pm25_jump(_local(2026, 12, 1)) == fetchers.SPIKE_PM25_JUMP_COLD
    assert fetchers._get_spike_pm25_jump(_local(2026, 7, 1)) == fetchers.SPIKE_PM25_JUMP_DEFAULT
    assert fetchers._get_warn_aqi_jump(_local(2026, 1, 15)) == fetchers.WARN_AQI_JUMP_COLD
    assert fetchers._get_warn_aqi_jump(_local(2026, 5, 15)) == fetchers.WARN_AQI_JUMP_DEFAULT


def test_jump_spike_detected_against_an_hour_ago():
    now = _local(2026, 7, 1, 12)
    history = [{"ts": now - 3600, "pm2_5": 20.0}, {"ts": now - 7200, "pm2_5": 500.0}]

    assert fetchers._is_jump_spike("z", now, 250.0, history) == 230.0
    assert fetchers._is_jump_spike("z", now, 200.0, history) is None


def test_jump_spike_needs_a_nearby_comparison():
    now = _local(2026, 7, 1, 12)
    history = [{"ts": now - 3600 - fetchers.SPIKE_LOOKBACK_TOLERANCE, "pm2_5": 1.0}]

    assert fetchers._is_jump_spike("z", now, 900.0, history) is None
    assert fetchers._is_jump_spike("z", now, 900.0, []) is None


def test_jump_spike_off_during_festivals():
    now = _local(2026, 11, 8, 22)
    history = [{"ts": now - 3600, "pm2_5": 20.0}]

    assert fetchers._is_jump_spike("z", now, 900.0, history) is None


def test_spike_grace_expires_and_clears():
    fetchers._SPIKE_CACHE["z_a"] = 1000.0

    assert fetchers._spike_grace_remaining("z_a", 1000.0 + 600) == 50
    assert fetchers._spike_grace_remaining("z_a", 1000.0 + fetchers.SPIKE_GRACE_PERIOD) is None
    assert "z_a" not in fetchers._SPIKE_CACHE
    assert fetchers._spike_grace_remaining("never", 0) is None


def test_openmeteo_gases_walks_back_over_nulls():
    now = int(time.time() // 3600) * 3600
    times = [now - 7200, now - 3600, now]
    reply = FakeResponse({
        "hourly": {
            "time": times,
            "nitrogen_dioxide": [10.0, 11.0, None],
            "carbon_monoxide": [200.0, None, None],
            "methane": [None, None, None],
        }
    })

    points, current = fetchers._extract_openmeteo_gases(reply)

    assert current == {"no2": 11.0, "co": 200.0}
    assert {"ts": now - 3600, "param": "no2", "val": 11.0} in points
    assert not any(p["param"] == "ch4" for p in points)


def test_openmeteo_gases_failure_is_empty():
    assert fetchers._extract_openmeteo_gases(RuntimeError("boom")) == ([], {})
    assert fetchers._extract_openmeteo_gases(FakeResponse({}, status_code=500)) == ([], {})


def test_airgradient_field_helpers():
    assert fetchers._first_present({"a": None, "b": 0, "c": 5}, "a", "b", "c") == 0
    assert fetchers._first_present({}, "a") is None
    assert fetchers._as_float("12.5") == 12.5
    assert fetchers._as_float("n/a") is None
    assert fetchers._as_float(None) is None
    assert fetchers._raw_fields({"pm02": 30, "pm10": "40", "atmp": None, "rhum": 55}) == {
        "pm2_5_raw": 30.0,
        "pm10_raw": 40.0,
        "temp_raw": None,
        "humidity_raw": 55.0,
    }


def test_parse_airgradient_timestamp():
    assert fetchers._parse_ag_timestamp("2026-11-14T10:00:00.000Z", 1.0) == 1794650400.0
    assert fetchers._parse_ag_timestamp("garbage", 7.0) == 7.0
    assert fetchers._parse_ag_timestamp(None, 7.0) == 7.0


def test_resolve_airgradient_token(monkeypatch):
    monkeypatch.setenv("SOME_AG_TOKEN", "secret")

    assert fetchers._resolve_airgradient_token(None) == (None, None)
    assert fetchers._resolve_airgradient_token({}) == (None, None)
    assert fetchers._resolve_airgradient_token({"token_env_var": "AG_PUBLIC"}) == ("PUBLIC_TOKEN", "AG_PUBLIC")
    assert fetchers._resolve_airgradient_token({"token_env_var": "SOME_AG_TOKEN"}) == ("secret", "SOME_AG_TOKEN")


def test_downsample_keeps_last_reading_per_hour():
    hour = _local_hour() - 7200
    data = [
        {"ts": hour + 60, "pm2_5": 10.0, "pm10": 20.0},
        {"ts": hour + 1800, "pm2_5": 40.0, "pm10": 50.0, "pm2_5_raw": 60.0},
        {"ts": hour + 3700, "pm2_5": 5.0, "pm10": 5.0},
    ]

    out = fetchers._downsample_to_hourly(data)

    assert [p["ts"] for p in out] == [hour, hour + 3600]
    assert out[0]["pm2_5"] == 40.0
    assert out[0]["us_aqi"] == calculate_overall_aqi({"pm2_5": 40.0, "pm10": 50.0})["us_aqi"]
    assert out[0]["us_aqi_raw"] == get_us_aqi("pm2_5", 60.0)
    assert out[1]["aqi_raw"] is None
    assert fetchers._downsample_to_hourly([]) == []


def test_24h_average_index_is_index_of_mean_concentration():
    history = [
        {"pm2_5": 5.0, "aqi": 8, "us_aqi": 27},
        {"pm2_5": 65.0, "aqi": 113, "us_aqi": 157},
    ]

    averages = fetchers._calculate_24h_averages(history)

    assert averages["pm2_5"] == 35.0
    assert averages["us_aqi"] == get_us_aqi("pm2_5", 35.0)
    assert averages["us_aqi"] != round((27 + 157) / 2)


def test_24h_average_without_particulates_keeps_mean_index():
    averages = fetchers._calculate_24h_averages([{"temp": 10.0, "aqi": 10}, {"temp": 12.0, "aqi": 21}])

    assert averages == {"temp": 11.0, "aqi": 16}
    assert fetchers._calculate_24h_averages([]) == {}


def test_fill_raw_hours_only_fills_gaps():
    hour = _local_hour() - 3600
    history = [
        {"ts": hour, "pm2_5": 10.0},
        {"ts": hour - 3600, "pm2_5": 10.0, "pm2_5_raw": 1.0},
    ]
    raw_rows = [
        {"ts": hour + 60, "pm2_5_raw": 20.0, "humidity_raw": 50.0},
        {"ts": hour + 600, "pm2_5_raw": 30.0, "humidity_raw": None},
        {"ts": hour - 3000, "pm2_5_raw": 99.0},
    ]

    out = fetchers._fill_raw_hours(history, raw_rows, "urban")

    assert out[0]["pm2_5_raw"] == 25.0
    assert out[0]["humidity_raw"] == 50.0
    assert out[0]["us_aqi_raw"] == get_us_aqi("pm2_5", 25.0)
    assert out[1]["pm2_5_raw"] == 1.0


def test_complete_node_map_marks_missing_sensors_offline():
    raw = {
        "nodes": {"Kanipora": {"pm2_5": 10.0}},
        "_node_statuses": [{"node": "Bemina", "status": "spike"}],
    }

    node_map = fetchers._complete_node_map("srinagar", raw, "hills")

    assert list(node_map) == database.zone_node_names("srinagar")
    assert node_map["Kanipora"] == {"pm2_5": 10.0, "status": "active"}
    assert node_map["Bemina"]["status"] == "spike"
    assert node_map["Hazratbal"]["status"] == "offline"
    assert node_map["Hazratbal"]["pm2_5"] is None


@pytest.mark.parametrize(
    "code, pm, expected",
    [
        (None, None, "cloudy"),
        (0, None, "clear"),
        (3, None, "cloudy"),
        (45, 20.0, "fog"),
        (45, 80.0, "smog"),
        (61, None, "rain"),
        (73, None, "snow"),
        (95, None, "thunderstorm"),
    ],
)
def test_classify_condition(code, pm, expected):
    assert classify_condition(code, pm) == expected


def test_weather_text_depends_on_season_when_clear():
    assert build_weather_text("clear", "winter") != build_weather_text("clear", "summer")
    assert "Rain" in build_weather_text("rain", "summer")


async def _no_legacy(zone_id):
    return None


def _live(history_key, **reading):
    fetchers._NODE_LIVE[history_key] = {
        "status": "active",
        "reading": {"timestamp": time.time() - 60, **reading},
        "gases": {},
        "warning": None,
        "fetched_at": time.time(),
    }


def test_sensor_payload_from_live_reading(monkeypatch, no_festivals):
    monkeypatch.setattr(fetchers, "_legacy_zone_payload", _no_legacy)
    _live("srinagar_Kanipora", pm2_5=40.0, pm10=60.0, temp=10.0, humidity=70.0, pm2_5_raw=70.0, pm10_raw=90.0)

    payload = asyncio.run(fetchers.get_sensor_zone_data("kanipora"))

    assert payload["zone_id"] == "kanipora"
    assert payload["kind"] == "sensor"
    assert payload["sensor_status"] == "active"
    assert payload["source"] == "airgradient + openmeteo"
    assert payload["us_aqi"] == calculate_overall_aqi({"pm2_5": 40.0, "pm10": 60.0})["us_aqi"]
    assert payload["raw_available"] is True
    assert payload["raw"]["us_aqi"] == calculate_overall_aqi({"pm2_5": 70.0, "pm10": 90.0})["us_aqi"]
    assert payload["warning"] is None
    assert payload["weather"] is None


def test_sensor_payload_is_cached(monkeypatch, no_festivals):
    monkeypatch.setattr(fetchers, "_legacy_zone_payload", _no_legacy)
    _live("srinagar_Kanipora", pm2_5=40.0, pm10=60.0)

    first = asyncio.run(fetchers.get_sensor_zone_data("kanipora"))
    _live("srinagar_Kanipora", pm2_5=400.0, pm10=60.0)

    assert asyncio.run(fetchers.get_sensor_zone_data("kanipora")) is first
    assert asyncio.run(fetchers.get_sensor_zone_data("kanipora", force_refresh=True))["readings"]["pm2_5"] == 400.0


def test_sensor_payload_warns_above_ceiling(monkeypatch, no_festivals):
    monkeypatch.setattr(fetchers, "_legacy_zone_payload", _no_legacy)
    _live("doda_town", pm2_5=fetchers.SPIKE_PM25_CEILING + 1, pm10=10.0)

    payload = asyncio.run(fetchers.get_sensor_zone_data("kashtigarh"))

    assert payload["warning"] == fetchers.WARNING_TEXT


def test_sensor_payload_falls_back_when_reading_is_stale(monkeypatch):
    monkeypatch.setattr(fetchers, "_legacy_zone_payload", _no_legacy)

    async def fake_openmeteo(lat, lon, zone_type):
        return {"current_comps": {"pm2_5": 12.0, "pm10": 30.0}, "history": []}

    monkeypatch.setattr(fetchers, "fetch_openmeteo_live", fake_openmeteo)
    _live("srinagar_Bemina", pm2_5=40.0, pm10=60.0)
    fetchers._NODE_LIVE["srinagar_Bemina"]["reading"]["timestamp"] = time.time() - fetchers.MAX_READING_AGE - 60

    payload = asyncio.run(fetchers.get_sensor_zone_data("bemina"))

    assert payload["source"] == "openmeteo air pollution api"
    assert payload["readings"]["pm2_5"] == 12.0
    assert payload["raw_available"] is False
    assert "offline" in payload["warning"]


def test_sensor_weather_reclassified_with_sensor_pm25():
    legacy = {"weather": {"weather_code": 45, "season": "winter", "temp": 3}}

    weather = fetchers._sensor_weather(legacy, 120.0)

    assert weather["condition"] == "smog"
    assert weather["temp"] == 3
    assert weather["text"] == build_weather_text("smog", "winter")
    assert fetchers._sensor_weather(None, 10.0) is None


def test_estimate_place_payload(monkeypatch):
    async def fake_zone(zone_id, name, lat, lon, zone_type, force_refresh=False):
        return {"zone_id": zone_id, "aqi": 42, "raw": "should be replaced"}

    monkeypatch.setattr(fetchers, "get_zone_data", fake_zone)

    payload = asyncio.run(fetchers.get_place_data("anantnag_city"))

    assert payload["kind"] == "estimate"
    assert payload["raw"] is None
    assert payload["raw_available"] is False
    assert payload["district"] == "Anantnag"
