# SPDX-License-Identifier: MIT
#
# Copyright (C) 2026 The Breathe Open Source Project
# Copyright (C) 2026 sidharthify <wednisegit@gmail.com>
# Copyright (C) 2026 FlashWreck <theghost3370@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# fetching. everything that talks to airgradient and open-meteo lives here, and
# everything that decides whether to believe what came back.
#
# the shape of a zone answer is always the same, whichever source produced it:
# {"current_comps": {...}, "history": [...]}. get_zone_data at the bottom picks
# a source, falls back when the ground sensors are unusable, and assembles the
# payload the api actually serves.

import os
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from app.core.config import ZONES, NODES_CONFIG, FESTIVALS, SENSOR_ZONES, ESTIMATE_ZONES
from app.core.conversions import calculate_overall_aqi
from app.core import database
from app.services.seasonal import season_for_month
from app.services.weather import get_zone_weather, classify_condition, build_weather_text

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
AG_BASE_URL = "https://api.airgradient.com/public/api/v1"

# built payloads, kept per process. this is in front of redis, not instead of
# it: with several workers each one warms its own copy.
_RAM_CACHE = {}

# zone_id + node name -> when that node last produced a spike.
_SPIKE_CACHE = {}

# history key of a sensor -> its last reading and status.
_NODE_LIVE = {}

CACHE_DURATION = 900
SPIKE_GRACE_PERIOD = 3600

# a reading older than this is treated as the sensor being down rather than as
# the current state of the air.
MAX_READING_AGE = 3600

# absolute ceilings. a real reading in these zones does not reach them, so
# anything above is the sensor being blown on, sprayed, or on fire.
SPIKE_PM25_CEILING = 600
SPIKE_PM10_CEILING = 900

# cold/heating months where temperature inversions and evening heating
# smoke cause genuine rapid rises in particulates.
COLD_SEASON_MONTHS = {10, 11, 12, 1, 2}  # October through February

# a jump this large inside an hour is local interference rather than weather.
SPIKE_PM25_JUMP_DEFAULT = 200
SPIKE_PM25_JUMP_COLD = 300
SPIKE_PM25_JUMP = SPIKE_PM25_JUMP_DEFAULT

# how far from exactly one hour ago a stored reading may sit and still be used
# as the comparison point for that jump.
SPIKE_LOOKBACK_TOLERANCE = 5400

# the index moving this far in an hour gets a warning attached to the payload.
WARN_AQI_JUMP_DEFAULT = 150
WARN_AQI_JUMP_COLD = 200
WARN_AQI_JUMP = WARN_AQI_JUMP_DEFAULT


def _get_spike_pm25_jump(reading_ts: Optional[float] = None) -> float:
    """Return the PM2.5 1-hour jump threshold based on the month of the reading."""
    month = datetime.fromtimestamp(reading_ts).month if reading_ts else datetime.now().month
    return SPIKE_PM25_JUMP_COLD if month in COLD_SEASON_MONTHS else SPIKE_PM25_JUMP_DEFAULT


def _get_warn_aqi_jump(current_ts: Optional[float] = None) -> float:
    """Return the AQI 1-hour jump threshold for advisory warnings based on month."""
    month = datetime.fromtimestamp(current_ts).month if current_ts else datetime.now().month
    return WARN_AQI_JUMP_COLD if month in COLD_SEASON_MONTHS else WARN_AQI_JUMP_DEFAULT


# festival dates where firecracker emissions cause actually real extreme spikes.
# during these windows, the 1 hour jump checks are bypassed and ceilings are raised
# to the hardware limit of the PM sensor.
_FESTIVAL_DATES = {
    date_str
    for dates in FESTIVALS.get("diwali_windows", {}).values()
    for date_str in dates
}
FESTIVAL_PM25_CEILING = 999
FESTIVAL_PM10_CEILING = 999


def is_festival_window(ts: Optional[float] = None) -> bool:
    """Return True if the timestamp falls on a festival date."""
    dt = datetime.fromtimestamp(ts) if ts else datetime.now()
    return dt.strftime("%Y-%m-%d") in _FESTIVAL_DATES


def _get_spike_ceilings(ts: Optional[float] = None) -> Tuple[float, float]:
    """Return (pm25_ceiling, pm10_ceiling) based on whether it is a festival day."""
    if is_festival_window(ts):
        return FESTIVAL_PM25_CEILING, FESTIVAL_PM10_CEILING
    return SPIKE_PM25_CEILING, SPIKE_PM10_CEILING

# gases come from open-meteo rather than the sensors, which measure particulates
# only.
GAS_PARAMS = {
    "ch4": "methane",
    "no2": "nitrogen_dioxide",
    "so2": "sulphur_dioxide",
    "co": "carbon_monoxide",
}

OM_GAS_PARAMS = {
    "hourly": "methane,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide",
    "timezone": "auto",
    "timeformat": "unixtime",
    "past_days": 1,
}

WARNING_TEXT = (
    "Warning: Unnatural spikes in sensors could be influenced by other "
    "atmospheric factors at the moment and this may not reflect the actual "
    "readings of the region"
)


class SpikeProtectionError(ValueError):
    """Raised when nodes are excluded due to spike protection rather than being offline."""

    def __init__(self, message: str, spike_warning: str):
        super().__init__(message)
        self.spike_warning = spike_warning


def _extract_openmeteo_gases(om_resp: Any) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Pull hourly gas points and a current value per gas out of an open-meteo reply.

    Returns (history points, current values). Both come back empty if the
    request failed, since gases are supplementary and never worth failing over.
    """
    if isinstance(om_resp, Exception) or om_resp.status_code != 200:
        return [], {}

    hourly = om_resp.json().get("hourly", {})
    times = hourly.get("time", [])

    series = {param: hourly.get(api_name, []) for param, api_name in GAS_PARAMS.items()}

    points = []
    for i, t in enumerate(times):
        for param, vals in series.items():
            if i < len(vals) and vals[i] is not None:
                points.append({"ts": t, "param": param, "val": vals[i]})

    current = {}
    if times:
        now_ts = datetime.now().timestamp()
        closest_ts = min(times, key=lambda t: abs(t - now_ts))
        idx = times.index(closest_ts)

        for param, vals in series.items():
            # the newest hours are often still null, so walk backwards a few
            # steps rather than reporting nothing.
            for step in range(0, 6):
                check_idx = idx - step
                if 0 <= check_idx < len(vals) and vals[check_idx] is not None:
                    current[param] = vals[check_idx]
                    break

    return points, current


def _spike_grace_remaining(node_cache_key: str, current_time: float) -> Optional[int]:
    """Minutes left on a node's spike grace period, or None if it is clear.

    Clears the entry as a side effect once the period has run out, so a node
    that has settled comes back on its own.
    """
    if node_cache_key not in _SPIKE_CACHE:
        return None

    time_since_spike = current_time - _SPIKE_CACHE[node_cache_key]

    if time_since_spike < SPIKE_GRACE_PERIOD:
        return int((SPIKE_GRACE_PERIOD - time_since_spike) / 60)

    del _SPIKE_CACHE[node_cache_key]
    return None


def _is_jump_spike(
    node_zone_id: str,
    reading_ts: float,
    pm25_val: float,
    history_24h: Optional[List[Dict[str, Any]]] = None,
) -> Optional[float]:
    """Compare a reading with roughly an hour ago. Returns the jump if it is a spike.

    Callers that already hold the last day of readings pass them in, since this
    runs once per node and the fetch is not free.
    """
    if is_festival_window(reading_ts):
        return None
    if history_24h is None:
        history_24h = database.get_history(node_zone_id, hours=24)

    if not history_24h:
        return None

    target_ts = reading_ts - 3600
    closest_reading = min(history_24h, key=lambda h: abs(h["ts"] - target_ts))

    # if nothing was stored near enough to an hour ago there is nothing
    # meaningful to compare against.
    if abs(closest_reading["ts"] - target_ts) >= SPIKE_LOOKBACK_TOLERANCE:
        return None

    pm25_jump = pm25_val - closest_reading["pm2_5"]
    spike_jump_limit = _get_spike_pm25_jump(reading_ts)
    if pm25_jump > spike_jump_limit:
        return pm25_jump

    return None


def _first_present(entry: Dict[str, Any], *keys: str) -> Any:
    """The first of these fields the sensor actually sent. A reading of 0 counts."""
    for key in keys:
        if entry.get(key) is not None:
            return entry[key]
    return None


def _as_float(value: Any) -> Optional[float]:
    """A reading as a float, or None when the sensor did not send one."""
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_fields(entry: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """The uncorrected values airgradient sends next to the corrected ones."""
    return {
        "pm2_5_raw": _as_float(entry.get("pm02")),
        "pm10_raw": _as_float(entry.get("pm10")),
        "temp_raw": _as_float(entry.get("atmp")),
        "humidity_raw": _as_float(entry.get("rhum")),
    }


def _remember_node(
    history_key: str,
    status: str,
    reading: Optional[Dict[str, Any]] = None,
    gases: Optional[Dict[str, Any]] = None,
    warning: Optional[str] = None,
) -> None:
    """Keep the last thing each sensor told us, for the per sensor zones."""
    _NODE_LIVE[history_key] = {
        "status": status,
        "reading": reading,
        "gases": gases or {},
        "warning": warning,
        "fetched_at": datetime.now().timestamp(),
    }


def _parse_ag_timestamp(raw: Optional[str], fallback: float) -> float:
    """Read airgradient's ISO timestamp, falling back to now if it is unusable."""
    if not raw:
        return fallback

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return fallback


async def fetch_airgradient_history(
    client: httpx.AsyncClient,
    loc_id: int,
    token: str,
) -> List[Dict[str, Any]]:
    """One day of past readings for a location, used only to refill an empty database."""
    url = f"{AG_BASE_URL}/locations/{loc_id}/measures/past"
    params = {"token": token, "period": "1day"}

    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            print(f"AG History Failed: {r.status_code}")
            return []

        history = []
        for entry in r.json():
            ts = entry.get("timestamp", 0)

            # airgradient has returned both seconds and milliseconds over the
            # years. anything past this is far in the future as seconds, so it
            # can only be milliseconds.
            if ts > 9999999999:
                ts = ts / 1000

            # the corrected values are the calibrated ones and are preferred
            # wherever the sensor reports them.
            pm25 = _first_present(entry, "pm02_corrected", "pm02")
            pm10 = _first_present(entry, "pm10_corrected", "pm10")

            if pm25 is not None:
                history.append({
                    "ts": ts,
                    "pm2_5": float(pm25),
                    "pm10": float(pm10) if pm10 else 0.0,
                    **_raw_fields(entry),
                })

        return history

    except Exception as e:
        print(f"AG History Fetch Error: {e}")
        return []


async def ensure_history_exists(zone_id: str, loc_id: int, token: str) -> None:
    """Refill a zone from the airgradient api if we hold nothing recent for it."""
    existing_data = database.get_history(zone_id, hours=1)
    if existing_data:
        return

    print(f" DB empty for {zone_id}. Refilling from AirGradient API...")

    async with httpx.AsyncClient() as client:
        history = await fetch_airgradient_history(client, loc_id, token)

        readings = [
            {
                "zone_id": zone_id,
                "pm2_5": pt["pm2_5"],
                "pm10": pt["pm10"],
                "pm2_5_raw": pt["pm2_5_raw"],
                "pm10_raw": pt["pm10_raw"],
                "temp_raw": pt["temp_raw"],
                "humidity_raw": pt["humidity_raw"],
                "timestamp": pt["ts"],
            }
            for pt in history
        ]

        database.save_readings(readings)
        print(f" Refilled {len(readings)} records for {zone_id}")


def _get_merged_history(zone_id: str, om_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stitch our own readings over the top of open-meteo's, hour by hour.

    Open-meteo fills in the time before a sensor existed and supplies the gases
    throughout. Once the sensor has readings for an hour, they win outright.
    """
    local_data = database.get_history(zone_id, hours=24)

    sensor_start_ts = None
    if local_data:
        local_data.sort(key=lambda x: x["ts"])

        # the hour our first real reading landed in. everything from here on is
        # sensor territory and open-meteo is not allowed to contribute.
        first_real_ts = local_data[0]["ts"]
        dt = datetime.fromtimestamp(first_real_ts)
        sensor_start_ts = dt.replace(minute=0, second=0, microsecond=0).timestamp()

    history_buckets = {}

    for pt in om_points:
        ts = pt["ts"]

        if sensor_start_ts and ts >= sensor_start_ts:
            continue

        if ts not in history_buckets:
            history_buckets[ts] = {}

        history_buckets[ts][pt["param"]] = pt["val"]

    if local_data:
        for pt in local_data:
            dt = datetime.fromtimestamp(pt["ts"])
            hour_ts = dt.replace(minute=0, second=0, microsecond=0).timestamp()

            if hour_ts not in history_buckets:
                history_buckets[hour_ts] = {}

            history_buckets[hour_ts]["pm2_5"] = pt["pm2_5"]
            history_buckets[hour_ts]["pm10"] = pt["pm10"]

            if "temp" in pt:
                history_buckets[hour_ts]["temp"] = pt["temp"]

            if "humidity" in pt:
                history_buckets[hour_ts]["humidity"] = pt["humidity"]

    now_ts = datetime.now().timestamp()

    # a zone with a sensor shows the sensor era only. a zone without one gets
    # the last day of estimates.
    clip_start_ts = now_ts - (24 * 3600)
    if sensor_start_ts:
        clip_start_ts = sensor_start_ts

    has_sensor_data = bool(local_data)
    final_history = []

    for ts in sorted(history_buckets.keys()):
        if ts < clip_start_ts or ts > now_ts:
            continue

        hour_comps = history_buckets[ts]

        # inside the sensor era an hour of gases with no particulates is a hole,
        # not a data point.
        if has_sensor_data and "pm2_5" not in hour_comps:
            continue

        try:
            aqi_res = calculate_overall_aqi(hour_comps, zone_type="urban")
        except Exception:
            continue

        final_history.append({
            "ts": int(ts),
            "aqi": aqi_res["aqi"],
            "us_aqi": aqi_res.get("us_aqi", 0),
            "pm2_5": hour_comps.get("pm2_5"),
            "pm10": hour_comps.get("pm10"),
            "temp": hour_comps.get("temp"),
            "humidity": hour_comps.get("humidity"),
        })

    return final_history


def _downsample_to_hourly(
    data: List[Dict[str, Any]],
    zone_type: str = "urban",
) -> List[Dict[str, Any]]:
    """Collapse raw readings to one per hour. The last reading in an hour wins."""
    if not data:
        return []

    buckets = {}
    for pt in data:
        dt = datetime.fromtimestamp(pt["ts"])
        hour_ts = int(dt.replace(minute=0, second=0, microsecond=0).timestamp())

        hour_comps = {
            "pm2_5": pt["pm2_5"],
            "pm10": pt["pm10"],
            "temp": pt.get("temp"),
            "humidity": pt.get("humidity"),
        }

        try:
            aqi_res = calculate_overall_aqi(hour_comps, zone_type=zone_type)
        except Exception:
            # keep the concentrations even when the index cannot be worked out,
            # the chart is drawn from those anyway.
            aqi_res = {"aqi": 0, "us_aqi": 0}

        raw_comps = {
            "pm2_5_raw": pt.get("pm2_5_raw"),
            "pm10_raw": pt.get("pm10_raw"),
            "temp_raw": pt.get("temp_raw"),
            "humidity_raw": pt.get("humidity_raw"),
        }

        raw_aqi = {"aqi": None, "us_aqi": None}
        if raw_comps["pm2_5_raw"] is not None:
            try:
                raw_aqi = calculate_overall_aqi(
                    {"pm2_5": raw_comps["pm2_5_raw"], "pm10": raw_comps["pm10_raw"] or 0.0},
                    zone_type=zone_type,
                )
            except Exception:
                raw_aqi = {"aqi": None, "us_aqi": None}

        buckets[hour_ts] = {
            "ts": hour_ts,
            **hour_comps,
            "aqi": aqi_res.get("aqi", 0),
            "us_aqi": aqi_res.get("us_aqi", 0),
            **raw_comps,
            "aqi_raw": raw_aqi.get("aqi"),
            "us_aqi_raw": raw_aqi.get("us_aqi"),
        }

    return [buckets[ts] for ts in sorted(buckets.keys())]


async def fetch_airgradient_common(
    zone_id: str,
    loc_id: int,
    token: str,
    lat: float,
    lon: float,
    zone_type: str = "urban",
    node_name: str = "Node 1",
) -> Dict[str, Any]:
    """Read a single sensor zone, plus gases from open-meteo."""
    if not token:
        raise HTTPException(status_code=500, detail=f"Missing AG Token for {zone_id}")

    # the public world endpoint has no history behind it, so there is nothing to
    # refill from.
    if token != "PUBLIC_TOKEN":
        await ensure_history_exists(zone_id, loc_id, token)

    async with httpx.AsyncClient(timeout=20) as client:
        if token == "PUBLIC_TOKEN":
            curr_url = f"{AG_BASE_URL}/world/locations/measures/current"
        else:
            curr_url = f"{AG_BASE_URL}/locations/{loc_id}/measures/current?token={token}"

        om_params = {"latitude": lat, "longitude": lon, **OM_GAS_PARAMS}

        ag_resp, om_resp = await asyncio.gather(
            client.get(curr_url),
            client.get(AIR_QUALITY_URL, params=om_params),
        )

    current_comps = {}

    if ag_resp.status_code == 200:
        d = ag_resp.json()

        # the public endpoint answers with every location in the world, so the
        # one we asked about has to be picked out.
        if token == "PUBLIC_TOKEN" and isinstance(d, list):
            d = next((s for s in d if s.get("locationId") == loc_id), {})

        pm25 = _first_present(d, "pm02_corrected", "pm02")
        pm10 = _first_present(d, "pm10_corrected", "pm10")

        # the api sometimes hands these back as strings, hence the float() calls
        # everywhere below.
        temp_val = _first_present(d, "atmp_corrected", "atmp")
        humid_val = _first_present(d, "rhum_corrected", "rhum")
        raw_vals = _raw_fields(d)

        current_time = datetime.now().timestamp()
        node_cache_key = f"{zone_id}_{node_name}"
        spike_warning = None

        remaining_minutes = _spike_grace_remaining(node_cache_key, current_time)
        if remaining_minutes is not None:
            spike_warning = (
                f"{node_name}: excluded due to recent spike "
                f"(grace period: {remaining_minutes} mins remaining)"
            )
            pm25 = None
            pm10 = None

        # _ag_timestamp is only set when the sensor gave us a timestamp we could
        # read. get_zone_data treats its absence as "cannot tell how old this
        # is" and skips the staleness check, which is the right call.
        reading_ts = _parse_ag_timestamp(d.get("timestamp"), current_time)
        if reading_ts != current_time:
            current_comps["_ag_timestamp"] = reading_ts

        if pm25 is not None:
            pm25_val = float(pm25)
            pm10_val = float(pm10) if pm10 is not None else 0.0

            pm25_ceil, pm10_ceil = _get_spike_ceilings(reading_ts)
            if pm25_val > pm25_ceil or pm10_val > pm10_ceil:
                spike_warning = (
                    f"{node_name}: absolute threshold exceeded "
                    f"(PM2.5={pm25_val:.0f} or PM10={pm10_val:.0f})"
                )
                _SPIKE_CACHE[node_cache_key] = current_time
                pm25 = None
                pm10 = None

            else:
                pm25_jump = _is_jump_spike(zone_id, reading_ts, pm25_val)
                if pm25_jump is not None:
                    spike_warning = (
                        f"{node_name}: sudden spike detected "
                        f"(PM2.5 jumped +{int(pm25_jump)} in 1 hour)"
                    )
                    _SPIKE_CACHE[node_cache_key] = current_time
                    pm25 = None
                    pm10 = None

        current_comps["pm2_5"] = float(pm25) if pm25 is not None else None
        current_comps["pm10"] = float(pm10) if pm10 is not None else None
        current_comps["temp"] = float(temp_val) if temp_val is not None else None
        current_comps["humidity"] = float(humid_val) if humid_val is not None else None

        if spike_warning:
            current_comps["_spike_warning"] = spike_warning

        # only readings that survived the spike checks are worth storing.
        if pm25 is not None:
            database.save_readings([{
                "zone_id": zone_id,
                "pm2_5": float(pm25),
                "pm10": float(pm10) if pm10 else 0.0,
                "temp": float(temp_val) if temp_val is not None else None,
                "humidity": float(humid_val) if humid_val is not None else None,
                **raw_vals,
                "timestamp": reading_ts,
            }])

    else:
        print(f"AG Live Fetch Failed for {zone_id}: {ag_resp.status_code}")

    om_points, gas_comps = _extract_openmeteo_gases(om_resp)
    current_comps.update(gas_comps)

    if ag_resp.status_code != 200:
        _remember_node(zone_id, "offline")
    elif current_comps.get("pm2_5") is None:
        _remember_node(
            zone_id,
            "spike_detected" if current_comps.get("_spike_warning") else "no_data",
            gases=gas_comps,
            warning=current_comps.get("_spike_warning"),
        )
    else:
        _remember_node(
            zone_id,
            "active",
            reading={
                "pm2_5": current_comps["pm2_5"],
                "pm10": current_comps.get("pm10"),
                "temp": current_comps.get("temp"),
                "humidity": current_comps.get("humidity"),
                "timestamp": current_comps.get("_ag_timestamp"),
                **raw_vals,
            },
            gases=gas_comps,
        )

    history = _get_merged_history(zone_id, om_points)

    node_history_24h = database.get_history(zone_id, hours=24)
    downsampled_history = _downsample_to_hourly(node_history_24h, zone_type=zone_type)

    if current_comps.get("pm2_5") is not None:
        try:
            aqi_data = calculate_overall_aqi(current_comps, zone_type=zone_type)
        except Exception:
            aqi_data = {"aqi": 0, "us_aqi": 0}

        # a single sensor zone still reports a nodes map, so the clients have one
        # shape to handle rather than two.
        current_comps["nodes"] = {
            node_name: {
                "pm2_5": current_comps.get("pm2_5"),
                "pm10": current_comps.get("pm10", 0.0),
                "temp": current_comps.get("temp"),
                "humidity": current_comps.get("humidity"),
                "aqi": aqi_data.get("aqi", 0),
                "us_aqi": aqi_data.get("us_aqi", 0),
                "history": downsampled_history,
            }
        }
        current_comps["_node_count"] = 1
        current_comps["_total_nodes"] = 1
        current_comps["_node_statuses"] = [{"node": node_name, "status": "active"}]

    else:
        current_comps["nodes"] = {}
        current_comps["_node_count"] = 0
        current_comps["_total_nodes"] = 1
        status_val = "spike_detected" if spike_warning else "offline"
        current_comps["_node_statuses"] = [{"node": node_name, "status": status_val}]

    return {"current_comps": current_comps, "history": history}


def _resolve_node_token(
    node_cfg: Dict[str, Any], zone_token: Optional[str]
) -> Optional[str]:
    node_env = node_cfg.get("token_env_var")
    if node_env:
        if "PUBLIC" in node_env.upper():
            return "PUBLIC_TOKEN"
        return os.getenv(node_env) or zone_token
    return zone_token


async def fetch_multi_node_airgradient(
    zone_id: str,
    nodes: List[Dict],
    token: str,
    lat: float,
    lon: float,
    zone_type: str = "urban",
) -> Dict[str, Any]:
    """Read every sensor in a zone and average the ones worth believing.

    Each node is stored under its own "<zone>_<node name>" id as well, which is
    what lets the history endpoint rebuild a zone from its parts later.
    """
    if not token:
        raise HTTPException(status_code=500, detail=f"Missing AG Token for {zone_id}")

    current_time = datetime.now().timestamp()

    async with httpx.AsyncClient(timeout=20) as client:
        curr_tasks = []
        world_task = None

        for node in nodes:
            node_token = _resolve_node_token(node, token)
            if node_token == "PUBLIC_TOKEN":
                if world_task is None:
                    world_task = asyncio.create_task(
                        client.get(f"{AG_BASE_URL}/world/locations/measures/current")
                    )
                curr_tasks.append(world_task)
            else:
                curr_tasks.append(
                    client.get(
                        f"{AG_BASE_URL}/locations/{node['location_id']}"
                        f"/measures/current?token={node_token}"
                    )
                )

        om_params = {"latitude": lat, "longitude": lon, **OM_GAS_PARAMS}
        curr_tasks.append(client.get(AIR_QUALITY_URL, params=om_params))

        # return_exceptions so one dead sensor cannot take the whole zone down.
        results = await asyncio.gather(*curr_tasks, return_exceptions=True)

    # open-meteo was appended last, so it comes back last.
    om_resp = results[-1]
    sensor_responses = results[:-1]

    valid_readings = []
    node_statuses = []
    spike_warnings = []

    for i, resp in enumerate(sensor_responses):
        node_cfg = nodes[i]
        node_name = node_cfg.get("name", f"Node{i+1}")
        node_cache_key = f"{zone_id}_{node_name}"
        node_zone_id = f"{zone_id}_{node_name}"

        remaining_minutes = _spike_grace_remaining(node_cache_key, current_time)
        if remaining_minutes is not None:
            node_statuses.append({
                "node": node_name,
                "status": "grace_period",
                "remaining_minutes": remaining_minutes,
            })
            spike_warnings.append(
                f"{node_name}: excluded due to recent spike "
                f"(grace period: {remaining_minutes} mins remaining)"
            )
            _remember_node(node_zone_id, "grace_period", warning=spike_warnings[-1])
            continue

        if isinstance(resp, Exception):
            node_statuses.append({"node": node_name, "status": "error"})
            _remember_node(node_zone_id, "offline")
            continue

        if resp.status_code != 200:
            node_statuses.append({"node": node_name, "status": "offline"})
            _remember_node(node_zone_id, "offline")
            continue

        data = resp.json()

        node_token = _resolve_node_token(node_cfg, token)
        if node_token == "PUBLIC_TOKEN" and isinstance(data, list):
            sensor_data = next(
                (s for s in data if s.get("locationId") == node_cfg["location_id"]), None
            )
            if not sensor_data:
                node_statuses.append({"node": node_name, "status": "offline"})
                _remember_node(node_zone_id, "offline")
                continue
            data = sensor_data

        pm25 = _first_present(data, "pm02_corrected", "pm02")
        pm10 = _first_present(data, "pm10_corrected", "pm10")

        if pm25 is None:
            node_statuses.append({"node": node_name, "status": "no_data"})
            _remember_node(node_zone_id, "no_data")
            continue

        pm25 = float(pm25)
        pm10 = float(pm10) if pm10 is not None else None

        reading_ts = _parse_ag_timestamp(data.get("timestamp"), current_time)
        data_age = current_time - reading_ts if data.get("timestamp") else None

        # a node still answering with an hour old reading is not reporting, and
        # averaging it in would drag the zone towards stale air.
        if data_age and data_age > MAX_READING_AGE:
            node_statuses.append({
                "node": node_name,
                "status": "stale",
                "age_minutes": int(data_age / 60),
            })
            _remember_node(node_zone_id, "stale")
            continue

        pm25_val = pm25
        pm10_val = pm10 if pm10 else 0.0

        pm25_ceil, pm10_ceil = _get_spike_ceilings(reading_ts)
        if pm25_val > pm25_ceil or pm10_val > pm10_ceil:
            node_statuses.append({
                "node": node_name,
                "status": "spike_detected",
                "pm2_5": pm25_val,
                "pm10": pm10_val,
            })
            spike_warnings.append(
                f"{node_name}: absolute threshold exceeded "
                f"(PM2.5={pm25_val:.0f} or PM10={pm10_val:.0f})"
            )
            _SPIKE_CACHE[node_cache_key] = current_time
            _remember_node(node_zone_id, "spike_detected", warning=spike_warnings[-1])
            continue

        node_history_24h = database.get_history(node_zone_id, hours=24)

        pm25_jump = _is_jump_spike(node_zone_id, reading_ts, pm25_val, node_history_24h)
        if pm25_jump is not None:
            node_statuses.append({
                "node": node_name,
                "status": "spike_detected",
                "pm25_jump": int(pm25_jump),
            })
            spike_warnings.append(
                f"{node_name}: sudden spike detected "
                f"(PM2.5 jumped +{int(pm25_jump)} in 1 hour)"
            )
            _SPIKE_CACHE[node_cache_key] = current_time
            _remember_node(node_zone_id, "spike_detected", warning=spike_warnings[-1])
            continue

        temp_val = _first_present(data, "atmp_corrected", "atmp")
        humid_val = _first_present(data, "rhum_corrected", "rhum")

        valid_readings.append({
            "pm2_5": pm25_val,
            "pm10": pm10_val,
            "temp": float(temp_val) if temp_val is not None else None,
            "humidity": float(humid_val) if humid_val is not None else None,
            "timestamp": reading_ts,
            "node_name": node_name,
            **_raw_fields(data),
            "history": _downsample_to_hourly(node_history_24h, zone_type=zone_type),
        })
        node_statuses.append({"node": node_name, "status": "active"})

    # caught by get_zone_data, which falls the zone back to open-meteo.
    if not valid_readings:
        if spike_warnings:
            raise SpikeProtectionError(
                f"All {len(nodes)} sensor nodes excluded due to spikes",
                "; ".join(spike_warnings),
            )
        raise ValueError(f"All {len(nodes)} sensor nodes offline")

    database.save_readings([
        {
            "zone_id": f"{zone_id}_{r['node_name']}",
            "pm2_5": r["pm2_5"],
            "pm10": r["pm10"],
            "temp": r["temp"],
            "humidity": r["humidity"],
            "pm2_5_raw": r["pm2_5_raw"],
            "pm10_raw": r["pm10_raw"],
            "temp_raw": r["temp_raw"],
            "humidity_raw": r["humidity_raw"],
            "timestamp": r["timestamp"],
        }
        for r in valid_readings
    ])

    merged_pm25 = sum(r["pm2_5"] for r in valid_readings) / len(valid_readings)
    merged_pm10 = sum(r["pm10"] for r in valid_readings) / len(valid_readings)

    temp_readings = [r["temp"] for r in valid_readings if r["temp"] is not None]
    humidity_readings = [r["humidity"] for r in valid_readings if r["humidity"] is not None]

    merged_temp = sum(temp_readings) / len(temp_readings) if temp_readings else None
    merged_humid = sum(humidity_readings) / len(humidity_readings) if humidity_readings else None

    om_points, current_gas_comps = _extract_openmeteo_gases(om_resp)

    for r in valid_readings:
        _remember_node(
            f"{zone_id}_{r['node_name']}",
            "active",
            reading={
                "pm2_5": r["pm2_5"],
                "pm10": r["pm10"],
                "temp": r["temp"],
                "humidity": r["humidity"],
                "timestamp": r["timestamp"],
                "pm2_5_raw": r["pm2_5_raw"],
                "pm10_raw": r["pm10_raw"],
                "temp_raw": r["temp_raw"],
                "humidity_raw": r["humidity_raw"],
            },
            gases=current_gas_comps,
        )

    # each node gets its own index, computed against the same gas readings since
    # those are zone wide.
    node_map = {}
    for r in valid_readings:
        node_aqi = calculate_overall_aqi({**r, **current_gas_comps}, zone_type=zone_type)
        node_map[r["node_name"]] = {
            "pm2_5": r["pm2_5"],
            "pm10": r["pm10"],
            "temp": r["temp"],
            "humidity": r["humidity"],
            "aqi": node_aqi.get("aqi", 0),
            "us_aqi": node_aqi.get("us_aqi", 0),
            "history": r["history"],
        }

    current_comps = {
        "pm2_5": merged_pm25,
        "pm10": merged_pm10,
        "temp": merged_temp,
        "humidity": merged_humid,
        **current_gas_comps,
        "nodes": node_map,
        "_ag_timestamp": max(r["timestamp"] for r in valid_readings),
        "_node_count": len(valid_readings),
        "_total_nodes": len(nodes),
        "_node_statuses": node_statuses,
    }

    if spike_warnings:
        current_comps["_spike_warning"] = (
            f"Data from {len(valid_readings)} of {len(nodes)} sensors. "
            + "; ".join(spike_warnings)
        )

    # the zone average is stored under the plain zone id, alongside the per node
    # rows written above.
    database.save_readings([{
        "zone_id": zone_id,
        "pm2_5": merged_pm25,
        "pm10": merged_pm10,
        "temp": merged_temp,
        "humidity": merged_humid,
        "timestamp": current_time,
    }])

    history = _get_merged_history(zone_id, om_points)

    return {"current_comps": current_comps, "history": history}


async def fetch_openmeteo_live(lat: float, lon: float, zone_type: str) -> Dict[str, Any]:
    """Model estimates for a zone with no ground sensor, or as a fallback for one that is down."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm10,pm2_5,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,methane",
        "timezone": "auto",
        "timeformat": "unixtime",
        "past_days": 1,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(AIR_QUALITY_URL, params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="openmeteo request failed")

        hourly = r.json().get("hourly", {})
        times = hourly.get("time", [])

        if not times:
            raise HTTPException(status_code=404, detail="no openmeteo aq data found")

        # open-meteo publishes on the hour, so the nearest hour is the present.
        now_ts = datetime.now().timestamp()
        closest_ts = min(times, key=lambda t: abs(t - now_ts))
        target_idx = times.index(closest_ts)

        def comps_at(index: int) -> Dict[str, Any]:
            """Every pollutant at one hour, with the nulls dropped."""
            values = {
                "pm10": hourly.get("pm10", [])[index],
                "pm2_5": hourly.get("pm2_5", [])[index],
                "no2": hourly.get("nitrogen_dioxide", [])[index],
                "so2": hourly.get("sulphur_dioxide", [])[index],
                "co": hourly.get("carbon_monoxide", [])[index],
                "ch4": hourly.get("methane", [])[index],
            }
            return {k: v for k, v in values.items() if v is not None}

        current_comps = comps_at(target_idx)

        start_ts = now_ts - (24 * 3600)
        history = []

        for i, t in enumerate(times):
            if t < start_ts or t > now_ts:
                continue

            hour_comps = comps_at(i)

            try:
                aqi_res = calculate_overall_aqi(hour_comps, zone_type=zone_type)
            except Exception:
                continue

            history.append({
                "ts": times[i],
                "aqi": aqi_res["aqi"],
                "us_aqi": aqi_res.get("us_aqi", 0),
                "pm2_5": hour_comps.get("pm2_5"),
                "pm10": hour_comps.get("pm10"),
            })

        return {"current_comps": current_comps, "history": history}


def _calculate_24h_averages(
    history: List[Dict[str, Any]],
    zone_type: str = "urban",
) -> Dict[str, Any]:
    """Mean of the last day, per metric."""
    if not history:
        return {}

    metrics = {"pm2_5": [], "pm10": [], "temp": [], "humidity": [], "aqi": [], "us_aqi": []}

    for pt in history:
        for k in metrics.keys():
            if k in pt and pt[k] is not None:
                metrics[k].append(pt[k])

    averages = {}
    for k, vals in metrics.items():
        if not vals:
            continue

        avg = sum(vals) / len(vals)

        if k in ("aqi", "us_aqi"):
            averages[k] = int(round(avg))
        else:
            averages[k] = round(avg, 1)

    # the index of the average concentration, not the average of the hourly
    # indices. the index curve is piecewise linear, so those two are not the
    # same number and the first is the one that means something.
    if "pm2_5" in averages or "pm10" in averages:
        aqi_comps = {
            k: v
            for k, v in {"pm2_5": averages.get("pm2_5"), "pm10": averages.get("pm10")}.items()
            if v is not None
        }

        if aqi_comps:
            try:
                aqi_res = calculate_overall_aqi(aqi_comps, zone_type=zone_type)
                averages["aqi"] = aqi_res.get("aqi", averages.get("aqi", 0))
                if "us_aqi" in aqi_res:
                    averages["us_aqi"] = aqi_res["us_aqi"]
            except Exception:
                # keep the mean of the hourly indices worked out above.
                pass

    return averages


def _resolve_airgradient_token(zone_node_cfg: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Work out which token a zone should use. Returns (token, env var name)."""
    token_env_var = zone_node_cfg.get("token_env_var") if zone_node_cfg else None

    if not token_env_var:
        return None, None

    # some zones read from airgradient's open world feed, which needs no key.
    # the sentinel keeps the rest of the code from having to special case it.
    if "PUBLIC" in token_env_var.upper():
        return "PUBLIC_TOKEN", token_env_var

    return os.getenv(token_env_var), token_env_var


def _complete_node_map(
    zone_id: str, raw_comps: Dict[str, Any], zone_type: str
) -> Optional[Dict[str, Any]]:
    """Every configured sensor of a zone in config order, offline ones included."""
    node_names = database.zone_node_names(zone_id)
    if not node_names:
        return raw_comps.get("nodes")

    live_nodes = raw_comps.get("nodes") or {}
    statuses = {
        entry["node"]: entry["status"]
        for entry in raw_comps.get("_node_statuses") or []
    }

    node_map = {}
    for name in node_names:
        if name in live_nodes:
            node_map[name] = {**live_nodes[name], "status": "active"}
            continue

        if len(node_names) > 1:
            history_key = f"{zone_id}_{name}"
        else:
            history_key = zone_id

        node_map[name] = {
            "pm2_5": None,
            "pm10": None,
            "temp": None,
            "humidity": None,
            "aqi": None,
            "us_aqi": None,
            "status": statuses.get(name, "offline"),
            "history": _downsample_to_hourly(
                database.get_history(history_key, hours=24), zone_type=zone_type
            ),
        }

    return node_map


async def get_zone_data(
    zone_id: str,
    zone_name: str,
    lat: float,
    lon: float,
    zone_type: str,
    force_refresh: bool = False,
):
    """Build the full payload for a zone, from cache where possible.

    Ground sensors are preferred wherever a zone has them. Anything that makes
    them unusable, a missing token, every node offline, a stale reading, falls
    the zone back to open-meteo estimates with a warning saying so.
    """
    cached_data = _RAM_CACHE.get(zone_id)
    current_time = datetime.now().timestamp()

    if cached_data and not force_refresh:
        last_fetched = cached_data.get("timestamp_unix", 0)
        if current_time - last_fetched < CACHE_DURATION:
            return cached_data

    try:
        sensor_offline_warning = None

        zone_info = ZONES.get(zone_id)
        if zone_info and zone_info.get("provider") == "airgradient":
            zone_node_cfg = NODES_CONFIG.get(zone_id)
            token, token_env_var = _resolve_airgradient_token(zone_node_cfg)

            if not zone_node_cfg or not token_env_var or not token:
                if not zone_node_cfg:
                    err_msg = f"No AirGradient configuration found for zone {zone_id} in nodes.json"
                elif not token_env_var:
                    err_msg = (
                        f"Missing 'token_env_var' configuration for AirGradient "
                        f"zone '{zone_id}' in nodes.json"
                    )
                else:
                    err_msg = (
                        f"AirGradient token environment variable '{token_env_var}' "
                        f"is not set/empty for zone '{zone_id}'"
                    )

                print(f"CRITICAL CONFIG ERROR for {zone_id}: {err_msg}. Falling back to Open-Meteo...")
                fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                source_name = "openmeteo air pollution api"
                sensor_offline_warning = (
                    "System configuration error: Missing AirGradient credentials. "
                    "Using estimates from Open-Meteo."
                )

            else:
                nodes = [n for n in zone_node_cfg.get("nodes", []) if n.get("enabled", True)]

                if not nodes:
                    print(
                        f"CRITICAL CONFIG ERROR for {zone_id}: All nodes are disabled. "
                        "Falling back to Open-Meteo..."
                    )
                    fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                    source_name = "openmeteo air pollution api"
                    sensor_offline_warning = (
                        "System configuration error: All nodes for this zone are "
                        "disabled. Using estimates from Open-Meteo."
                    )

                else:
                    try:
                        if len(nodes) > 1:
                            fetched_data = await fetch_multi_node_airgradient(
                                zone_id=zone_id,
                                nodes=nodes,
                                token=token,
                                lat=lat,
                                lon=lon,
                                zone_type=zone_type,
                            )
                            comps = fetched_data["current_comps"]
                            source_name = (
                                f"airgradient ({comps['_node_count']}/"
                                f"{comps['_total_nodes']} sensors) + openmeteo"
                            )

                        else:
                            config = nodes[0]
                            node_token = _resolve_node_token(config, token)
                            fetched_data = await fetch_airgradient_common(
                                zone_id=zone_id,
                                loc_id=config["location_id"],
                                token=node_token,
                                lat=lat,
                                lon=lon,
                                zone_type=zone_type,
                                node_name=config.get("name", "Node 1"),
                            )
                            source_name = "airgradient + openmeteo"

                        if "_spike_warning" in fetched_data["current_comps"]:
                            sensor_offline_warning = fetched_data["current_comps"]["_spike_warning"]

                        # these two raise on purpose, to drop into the fallback
                        # below rather than serving a zone with no reading in it.
                        if fetched_data["current_comps"].get("pm2_5") is None:
                            raise ValueError("No PM2.5 data from sensor")

                        ag_timestamp = fetched_data["current_comps"].get("_ag_timestamp")
                        if ag_timestamp:
                            data_age = current_time - ag_timestamp
                            if data_age > MAX_READING_AGE:
                                raise ValueError(
                                    f"Sensor data is stale ({int(data_age / 60)} minutes old)"
                                )

                    except SpikeProtectionError as e:
                        print(f"Spike protection active for {zone_id}: {e}, falling back to Open-Meteo")
                        fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                        source_name = "openmeteo air pollution api"
                        sensor_offline_warning = (
                            f"{e.spike_warning}. Using satellite-based estimates from Open-Meteo."
                        )

                    except Exception as e:
                        print(f"Sensor offline for {zone_id}: {e}, falling back to Open-Meteo")
                        fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                        source_name = "openmeteo air pollution api"
                        if sensor_offline_warning:
                            sensor_offline_warning = (
                                f"{sensor_offline_warning}. Using satellite-based estimates from Open-Meteo."
                            )
                        else:
                            sensor_offline_warning = (
                                "Physical sensor temporarily offline. Using "
                                "satellite-based estimates from Open-Meteo."
                            )

        else:
            fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
            source_name = "openmeteo air pollution api"

        raw_comps = fetched_data["current_comps"]
        history = fetched_data["history"]

        aqi_data = calculate_overall_aqi(raw_comps, zone_type=zone_type)
        current_aqi = aqi_data.get("aqi", 0)

        warning_msg = None

        # the zone average can clear the ceiling even when no single node did,
        # so it is checked again here on the merged concentrations.
        pm25_val = raw_comps.get("pm2_5", 0)
        pm10_val = raw_comps.get("pm10", 0)

        pm25_ceil, pm10_ceil = _get_spike_ceilings(current_time)
        if pm25_val > pm25_ceil or pm10_val > pm10_ceil:
            warning_msg = WARNING_TEXT

        def get_past_aqi(target_ts, history_list, tolerance=1800):
            """First history point within tolerance of a timestamp, if there is one."""
            for point in history_list:
                if abs(point["ts"] - target_ts) <= tolerance:
                    return point["aqi"]
            return None

        if history:
            val_1h = get_past_aqi(current_time - 3600, history)

            if val_1h is not None and not warning_msg:
                warn_jump_limit = _get_warn_aqi_jump(current_time)
                if (current_aqi - val_1h) > warn_jump_limit:
                    warning_msg = WARNING_TEXT

        # the offline notice goes first, since it explains where the numbers
        # underneath it came from.
        if sensor_offline_warning:
            if warning_msg:
                warning_msg = f"{sensor_offline_warning}\n\n{warning_msg}"
            else:
                warning_msg = sensor_offline_warning

        averages_24h = _calculate_24h_averages(history, zone_type)
        weather = await get_zone_weather(lat, lon, raw_comps.get("pm2_5"))

        if zone_info and zone_info.get("provider") == "airgradient":
            node_map = _complete_node_map(zone_id, raw_comps, zone_type)
        else:
            node_map = raw_comps.get("nodes")

        full_payload = {
            "zone_id": zone_id,
            "zone_name": zone_name,
            "source": source_name,
            "timestamp_unix": current_time,
            "coordinates": {"lat": lat, "lon": lon},
            "averages_24h": averages_24h,
            "history": history,
            "warning": warning_msg,
            "nodes": node_map,
            "weather": weather,
            **aqi_data,
        }

        _RAM_CACHE[zone_id] = full_payload
        return full_payload

    except Exception as e:
        # a stale answer beats an error page, so the last good payload is served
        # if we have one.
        print(f"Live fetch failed for {zone_id}: {e}")
        if cached_data:
            return cached_data
        raise e


RAW_KEYS = ("pm2_5_raw", "pm10_raw", "temp_raw", "humidity_raw")


def _fill_raw_hours(
    history: List[Dict[str, Any]],
    raw_rows: List[Dict[str, Any]],
    zone_type: str,
) -> List[Dict[str, Any]]:
    """Give hours that only have backfilled raw readings their raw values and raw index."""
    sums: Dict[int, Dict[str, float]] = {}
    counts: Dict[int, Dict[str, int]] = {}
    for row in raw_rows:
        dt = datetime.fromtimestamp(row["ts"])
        hour_ts = int(dt.replace(minute=0, second=0, microsecond=0).timestamp())
        hour_sums = sums.setdefault(hour_ts, {})
        hour_counts = counts.setdefault(hour_ts, {})
        for key in RAW_KEYS:
            value = row.get(key)
            if value is None:
                continue
            hour_sums[key] = hour_sums.get(key, 0.0) + value
            hour_counts[key] = hour_counts.get(key, 0) + 1

    for point in history:
        if point.get("pm2_5_raw") is not None:
            continue

        hour_sums = sums.get(point["ts"])
        if not hour_sums or "pm2_5_raw" not in hour_sums:
            continue

        for key, total in hour_sums.items():
            point[key] = round(total / counts[point["ts"]][key], 2)

        try:
            raw_aqi = calculate_overall_aqi(
                {"pm2_5": point["pm2_5_raw"], "pm10": point.get("pm10_raw") or 0.0},
                zone_type=zone_type,
            )
            point["aqi_raw"] = raw_aqi.get("aqi")
            point["us_aqi_raw"] = raw_aqi.get("us_aqi")
        except Exception:
            continue

    return history


def _raw_averages(history: List[Dict[str, Any]], zone_type: str) -> Dict[str, Any]:
    """24 hour means of the uncorrected readings, keyed like averages_24h."""
    raw_history = [
        {
            "pm2_5": pt.get("pm2_5_raw"),
            "pm10": pt.get("pm10_raw"),
            "temp": pt.get("temp_raw"),
            "humidity": pt.get("humidity_raw"),
            "aqi": pt.get("aqi_raw"),
            "us_aqi": pt.get("us_aqi_raw"),
        }
        for pt in history
        if pt.get("pm2_5_raw") is not None
    ]
    return _calculate_24h_averages(raw_history, zone_type)


def _sensor_weather(legacy: Optional[Dict[str, Any]], pm2_5: Optional[float]) -> Optional[Dict[str, Any]]:
    """The district's current weather, reclassified against this sensor's own PM2.5."""
    weather = (legacy or {}).get("weather")
    if not weather:
        return None

    condition = classify_condition(weather.get("weather_code"), pm2_5)
    season = weather.get("season") or season_for_month(datetime.now().month)

    return {
        **weather,
        "condition": condition,
        "season": season,
        "text": build_weather_text(condition, season),
    }


def _place_fields(place: Dict[str, Any]) -> Dict[str, Any]:
    """What every place payload carries about where it is."""
    return {
        "kind": place["kind"],
        "district": place.get("district"),
        "division": place.get("division"),
    }


async def _legacy_zone_payload(zone_id: str) -> Optional[Dict[str, Any]]:
    """The old zone a sensor belongs to, fetched if our copy has gone stale."""
    zone = ZONES[zone_id]
    try:
        return await get_zone_data(
            zone_id, zone["name"], zone["lat"], zone["lon"], zone.get("zone_type", "hills")
        )
    except Exception as e:
        print(f"Legacy zone {zone_id} unavailable for its sensors: {e}")
        return None


async def get_sensor_zone_data(sensor_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Build the payload for one sensor, served as a zone of its own."""
    sensor = SENSOR_ZONES[sensor_id]
    cached = _RAM_CACHE.get(sensor_id)
    current_time = datetime.now().timestamp()

    if cached and not force_refresh:
        if current_time - cached.get("built_at", 0) < CACHE_DURATION:
            return cached

    zone_type = sensor.get("zone_type", "hills")
    legacy = await _legacy_zone_payload(sensor["legacy_zone"])
    live = _NODE_LIVE.get(sensor["history_key"])

    reading = None
    if live and live["status"] == "active" and live.get("reading"):
        reading_ts = live["reading"].get("timestamp") or live["fetched_at"]
        if current_time - reading_ts <= MAX_READING_AGE:
            reading = {**live["reading"], "timestamp": reading_ts}

    try:
        if reading:
            gases = live.get("gases") or {}
            comps = {"pm2_5": reading["pm2_5"], "pm10": reading["pm10"] or 0.0, **gases}
            aqi_data = calculate_overall_aqi(comps, zone_type=zone_type)

            raw = None
            if reading.get("pm2_5_raw") is not None:
                raw_comps = {
                    "pm2_5": reading["pm2_5_raw"],
                    "pm10": reading.get("pm10_raw") or 0.0,
                    **gases,
                }
                raw_aqi = calculate_overall_aqi(raw_comps, zone_type=zone_type)
                raw = {
                    "pm2_5": reading["pm2_5_raw"],
                    "pm10": reading.get("pm10_raw"),
                    "temp": reading.get("temp_raw"),
                    "humidity": reading.get("humidity_raw"),
                    "aqi": raw_aqi.get("aqi"),
                    "us_aqi": raw_aqi.get("us_aqi"),
                    "main_pollutant": raw_aqi.get("main_pollutant"),
                    "us_main_pollutant": raw_aqi.get("us_main_pollutant"),
                    "concentrations_us_units": raw_aqi.get("concentrations_us_units"),
                }

            history = _fill_raw_hours(
                _downsample_to_hourly(
                    database.get_history(sensor["history_key"], hours=24), zone_type=zone_type
                ),
                database.get_raw_history(sensor["history_key"], hours=24),
                zone_type,
            )
            source_name = "airgradient + openmeteo"
            timestamp = reading["timestamp"]
            readings = {
                "pm2_5": reading["pm2_5"],
                "pm10": reading["pm10"],
                "temp": reading.get("temp"),
                "humidity": reading.get("humidity"),
            }
            status = "active"

            warning_msg = None
            pm25_ceil, pm10_ceil = _get_spike_ceilings(current_time)
            if reading["pm2_5"] > pm25_ceil or (reading["pm10"] or 0.0) > pm10_ceil:
                warning_msg = WARNING_TEXT

            hour_ago = [pt for pt in history if abs(pt["ts"] - (timestamp - 3600)) <= 1800]
            if hour_ago and not warning_msg:
                if aqi_data.get("aqi", 0) - hour_ago[0]["aqi"] > _get_warn_aqi_jump(current_time):
                    warning_msg = WARNING_TEXT

        else:
            fetched = await fetch_openmeteo_live(sensor["lat"], sensor["lon"], zone_type)
            comps = fetched["current_comps"]
            aqi_data = calculate_overall_aqi(comps, zone_type=zone_type)
            raw = None
            history = fetched["history"]
            source_name = "openmeteo air pollution api"
            timestamp = current_time
            readings = {
                "pm2_5": comps.get("pm2_5"),
                "pm10": comps.get("pm10"),
                "temp": None,
                "humidity": None,
            }
            status = live["status"] if live else "offline"

            if live and live.get("warning"):
                warning_msg = f"{live['warning']}. Using satellite-based estimates from Open-Meteo."
            else:
                warning_msg = (
                    "This sensor is offline right now. Showing satellite-based "
                    "estimates from Open-Meteo for its location."
                )

    except Exception as e:
        print(f"Sensor zone {sensor_id} failed: {e}")
        if cached:
            return cached
        raise e

    payload = {
        "zone_id": sensor_id,
        "zone_name": sensor["name"],
        **_place_fields(sensor),
        "location_id": sensor["location_id"],
        "sensor_status": status,
        "source": source_name,
        "timestamp_unix": timestamp,
        "built_at": current_time,
        "coordinates": {"lat": sensor["lat"], "lon": sensor["lon"]},
        "averages_24h": _calculate_24h_averages(history, zone_type),
        "averages_24h_raw": _raw_averages(history, zone_type) if raw else {},
        "history": history,
        "warning": warning_msg,
        "weather": _sensor_weather(legacy, readings["pm2_5"]),
        "readings": readings,
        "raw": raw,
        "raw_available": raw is not None,
        **aqi_data,
    }

    _RAM_CACHE[sensor_id] = payload
    return payload


async def get_place_data(place_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Serve a sensor or a modelled estimate zone by its id."""
    if place_id in SENSOR_ZONES:
        return await get_sensor_zone_data(place_id, force_refresh=force_refresh)

    place = ESTIMATE_ZONES[place_id]
    data = await get_zone_data(
        place["id"],
        place["name"],
        place["lat"],
        place["lon"],
        place.get("zone_type", "hills"),
        force_refresh=force_refresh,
    )
    return {
        **data,
        **_place_fields(place),
        "raw": None,
        "raw_available": False,
    }


RAW_BACKFILL_CHUNK = 10 * 86400
RAW_BACKFILL_CHUNKS_PER_PASS = 4


def _ag_basic_time(ts: float) -> str:
    """A unix time in the compact ISO form airgradient's past endpoint expects."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def _backfill_sensor_raw(client: httpx.AsyncClient, sensor: Dict[str, Any]) -> None:
    """Walk one sensor's raw history back towards its first stored reading, a few chunks at a time."""
    zone_cfg = NODES_CONFIG.get(sensor["legacy_zone"]) or {}
    node_cfg = next(
        (n for n in zone_cfg.get("nodes", []) if n.get("id") == sensor["id"]), None
    )
    if node_cfg is None:
        return

    zone_token, _ = _resolve_airgradient_token(zone_cfg)
    token = _resolve_node_token(node_cfg, zone_token)
    if not token or token == "PUBLIC_TOKEN":
        return

    key = sensor["history_key"]

    for _ in range(RAW_BACKFILL_CHUNKS_PER_PASS):
        window = database.raw_backfill_window(key)
        if window is None:
            return

        first, reached = window
        if reached <= first:
            return

        start = max(first, reached - RAW_BACKFILL_CHUNK)
        params = {"token": token, "from": _ag_basic_time(start), "to": _ag_basic_time(reached)}
        url = f"{AG_BASE_URL}/locations/{sensor['location_id']}/measures/past"

        try:
            r = await client.get(url, params=params)
        except Exception as e:
            print(f"Raw backfill fetch failed for {sensor['id']}: {e}")
            return

        if r.status_code != 200:
            print(f"Raw backfill for {sensor['id']} got {r.status_code}")
            return

        readings = []
        for entry in r.json() or []:
            ts = _parse_ag_timestamp(entry.get("timestamp"), 0)
            if ts <= 0:
                continue

            raw = _raw_fields(entry)
            if raw["pm2_5_raw"] is None:
                continue

            readings.append({"zone_id": key, "timestamp": ts, **raw})

        database.save_raw_backfill(readings)
        database.set_raw_backfill_reached(key, start)
        print(f"Raw backfill for {sensor['id']}: {len(readings)} readings back to {_ag_basic_time(start)}")


async def backfill_raw_history() -> None:
    """Fill in uncorrected readings for the time before we started storing them."""
    async with httpx.AsyncClient(timeout=30) as client:
        for sensor in SENSOR_ZONES.values():
            try:
                await _backfill_sensor_raw(client, sensor)
            except Exception as e:
                print(f"Raw backfill error for {sensor['id']}: {e}")


async def start_background_loop():
    """Standalone update loop. main.py runs its own, this is kept for scripts."""
    print("--- Background Scheduler Started ---")

    while True:
        try:
            await update_all_zones_background()
        except Exception as e:
            print(f"Error in background loop: {e}")

        await asyncio.sleep(CACHE_DURATION)


async def update_all_zones_background():
    """Refresh every zone in one pass, five at a time."""
    print(f"--- Updating Zones at {datetime.now()} ---")

    # open-meteo rate limits, and a burst of twenty five parallel requests is a
    # good way to get thirty zones worth of nothing.
    semaphore = asyncio.Semaphore(5)

    async def throttled_update(z):
        """Refresh one zone, reporting whether it worked rather than raising."""
        async with semaphore:
            try:
                await get_zone_data(
                    z["id"],
                    z["name"],
                    z["lat"],
                    z["lon"],
                    z.get("zone_type", "hills"),
                    force_refresh=True,
                )
                return True
            except Exception as e:
                print(f"Failed to update {z['id']}: {e}")
                return False

    results = await asyncio.gather(*[throttled_update(z) for z in ZONES.values()])

    success_count = sum(1 for r in results if r)
    print(f"--- Update Cycle Complete ({success_count}/{len(ZONES)} zones updated) ---")

    async def throttled_sensor(sensor_id):
        """Rebuild one sensor's payload from the pass that just finished."""
        async with semaphore:
            try:
                await get_sensor_zone_data(sensor_id, force_refresh=True)
                return True
            except Exception as e:
                print(f"Failed to update sensor {sensor_id}: {e}")
                return False

    sensor_results = await asyncio.gather(*[throttled_sensor(sid) for sid in SENSOR_ZONES])
    print(
        f"--- Sensors Rebuilt ({sum(1 for r in sensor_results if r)}/{len(SENSOR_ZONES)}) ---"
    )
