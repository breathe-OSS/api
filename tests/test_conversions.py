import pytest

from app.core.conversions import (
    _ugm3_to_ppb,
    calculate_overall_aqi,
    get_single_pollutant_aqi,
    get_us_aqi,
    linear_interpolate,
)


@pytest.mark.parametrize(
    "conc, expected",
    [
        (0.0, 0),
        (9.0, 50),
        (9.05, 50),
        (9.1, 51),
        (12.0, 56),
        (35.4, 100),
        (35.5, 101),
        (55.4, 150),
        (55.5, 151),
        (125.5, 201),
        (225.5, 301),
        (325.4, 500),
    ],
)
def test_us_pm25_breakpoints(conc, expected):
    assert get_us_aqi("pm2_5", conc) == expected


@pytest.mark.parametrize(
    "conc, expected",
    [
        (54.9, 50),
        (55, 51),
        (154, 100),
        (155, 101),
        (604, 500),
    ],
)
def test_us_pm10_truncates_to_integer(conc, expected):
    assert get_us_aqi("pm10", conc) == expected


def test_us_aqi_extrapolates_past_the_top_band():
    assert get_us_aqi("pm2_5", 500) == 847


def test_us_aqi_negative_is_zero():
    assert get_us_aqi("pm2_5", -3) == 0


def test_us_aqi_unknown_pollutant():
    assert get_us_aqi("ch4", 10) is None


def test_gas_conversion():
    assert _ugm3_to_ppb("no2", 46.0055) == pytest.approx(24.45)
    assert _ugm3_to_ppb("co", 1000) == pytest.approx(1000 * 24.45 / 28.010 / 1000)
    assert _ugm3_to_ppb("pm2_5", 10) is None


def test_us_co_goes_through_ppm():
    assert get_us_aqi("co", _ugm3_to_ppb("co", 1000)) == 9


@pytest.mark.parametrize(
    "conc, expected",
    [
        (0, 0),
        (30, 50),
        (30.9, 50),
        (31, 51),
        (60, 100),
        (75, 148),
        (250, 400),
        (380, 500),
    ],
)
def test_indian_pm25_breakpoints(conc, expected):
    assert get_single_pollutant_aqi("pm2_5", conc) == expected


def test_indian_aqi_unknown_pollutant():
    assert get_single_pollutant_aqi("ch4", 1.0) is None


def test_linear_interpolate_zero_width_band():
    assert linear_interpolate(5, (5, 5, 42, 99)) == 42


def test_overall_takes_worst_sub_index():
    result = calculate_overall_aqi({"pm2_5": 75, "pm10": 120})

    assert result["aqi"] == 148
    assert result["main_pollutant"] == "pm2_5"
    assert result["aqi_breakdown"] == {"pm2_5": 148, "pm10": 113}
    assert result["us_aqi"] == 164
    assert result["us_main_pollutant"] == "pm2_5"


def test_overall_normalises_key_spelling():
    spellings = ["pm2.5", "PM25", " pm2_5 "]

    results = [calculate_overall_aqi({key: 40.0}) for key in spellings]

    assert all(r["us_aqi"] == results[0]["us_aqi"] for r in results)
    assert all(r["aqi_breakdown"] == {"pm2_5": 66} for r in results)


def test_overall_ignores_unknown_keys_and_tracks_methane():
    result = calculate_overall_aqi({"pm2_5": 5, "ozone": 300, "methane": 1500})

    assert "ozone" not in result["concentrations_us_units"]
    assert result["concentrations_us_units"]["ch4"] == 1.5
    assert "ch4" not in result["aqi_breakdown"]


def test_overall_reports_co_in_mg_for_cpcb():
    result = calculate_overall_aqi({"carbon_monoxide": 2500})

    assert result["concentrations_us_units"]["co"] == 2.5
    assert "co" in result["aqi_breakdown"]


def test_overall_gas_can_win_us_index():
    result = calculate_overall_aqi({"pm2_5": 2, "nitrogen_dioxide": 400})

    assert result["us_main_pollutant"] == "no2"
    assert result["us_aqi"] > get_us_aqi("pm2_5", 2)


def test_overall_empty_input():
    result = calculate_overall_aqi({}, zone_type="hills")

    assert result["aqi"] == 0
    assert result["us_aqi"] == 0
    assert result["main_pollutant"] == "n/a"
    assert result["us_main_pollutant"] == "n/a"
    assert result["zone_applied"] == "hills"
