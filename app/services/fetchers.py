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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from app.core.config import ZONES, NODES_CONFIG
from app.core.conversions import calculate_overall_aqi
from app.core import database
from app.services.weather import get_zone_weather

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
AG_BASE_URL = "https://api.airgradient.com/public/api/v1"

# built payloads, kept per process. this is in front of redis, not instead of
# it: with several workers each one warms its own copy.
_RAM_CACHE = {}

# zone_id + node name -> when that node last produced a spike.
_SPIKE_CACHE = {}

CACHE_DURATION = 900
SPIKE_GRACE_PERIOD = 3600

# a reading older than this is treated as the sensor being down rather than as
# the current state of the air.
MAX_READING_AGE = 3600

# absolute ceilings. a real reading in these zones does not reach them, so
# anything above is the sensor being blown on, sprayed, or on fire.
SPIKE_PM25_CEILING = 650
SPIKE_PM10_CEILING = 600

# a jump this large inside an hour is local interference rather than weather.
SPIKE_PM25_JUMP = 200

# how far from exactly one hour ago a stored reading may sit and still be used
# as the comparison point for that jump.
SPIKE_LOOKBACK_TOLERANCE = 5400

# the index moving this far in an hour gets a warning attached to the payload.
WARN_AQI_JUMP = 150

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
    if pm25_jump > SPIKE_PM25_JUMP:
        return pm25_jump

    return None


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
            pm25 = entry.get("pm02_corrected") or entry.get("pm02")
            pm10 = entry.get("pm10_corrected") or entry.get("pm10")

            if pm25 is not None:
                history.append({
                    "ts": ts,
                    "pm2_5": float(pm25),
                    "pm10": float(pm10) if pm10 else 0.0,
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

        buckets[hour_ts] = {
            "ts": hour_ts,
            **hour_comps,
            "aqi": aqi_res.get("aqi", 0),
            "us_aqi": aqi_res.get("us_aqi", 0),
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

        pm25 = d.get("pm02_corrected") or d.get("pm02")
        pm10 = d.get("pm10_corrected") or d.get("pm10")

        # the api sometimes hands these back as strings, hence the float() calls
        # everywhere below.
        temp_val = d.get("atmp_corrected") or d.get("atmp")
        humid_val = d.get("rhum_corrected") or d.get("rhum")

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

            if pm25_val > SPIKE_PM25_CEILING or pm10_val > SPIKE_PM10_CEILING:
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
                "timestamp": reading_ts,
            }])

    else:
        print(f"AG Live Fetch Failed for {zone_id}: {ag_resp.status_code}")

    om_points, gas_comps = _extract_openmeteo_gases(om_resp)
    current_comps.update(gas_comps)

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
        current_comps["_node_statuses"] = [{"node": node_name, "status": "offline"}]

    return {"current_comps": current_comps, "history": history}


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
        for node in nodes:
            if token == "PUBLIC_TOKEN":
                url = f"{AG_BASE_URL}/world/locations/measures/current"
            else:
                url = (
                    f"{AG_BASE_URL}/locations/{node['location_id']}"
                    f"/measures/current?token={token}"
                )
            curr_tasks.append(client.get(url))

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
            continue

        if isinstance(resp, Exception):
            node_statuses.append({"node": node_name, "status": "error"})
            continue

        if resp.status_code != 200:
            node_statuses.append({"node": node_name, "status": "offline"})
            continue

        data = resp.json()

        if token == "PUBLIC_TOKEN" and isinstance(data, list):
            sensor_data = next(
                (s for s in data if s.get("locationId") == node_cfg["location_id"]), None
            )
            if not sensor_data:
                node_statuses.append({"node": node_name, "status": "offline"})
                continue
            data = sensor_data

        pm25 = data.get("pm02_corrected") or data.get("pm02")
        pm10 = data.get("pm10_corrected") or data.get("pm10")

        if pm25 is None:
            node_statuses.append({"node": node_name, "status": "no_data"})
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
            continue

        pm25_val = pm25
        pm10_val = pm10 if pm10 else 0.0

        if pm25_val > SPIKE_PM25_CEILING or pm10_val > SPIKE_PM10_CEILING:
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
            continue

        node_zone_id = f"{zone_id}_{node_name}"
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
            continue

        temp_val = data.get("atmp_corrected") or data.get("atmp")
        humid_val = data.get("rhum_corrected") or data.get("rhum")

        valid_readings.append({
            "pm2_5": pm25_val,
            "pm10": pm10_val,
            "temp": float(temp_val) if temp_val is not None else None,
            "humidity": float(humid_val) if humid_val is not None else None,
            "timestamp": reading_ts,
            "node_name": node_name,
            "history": _downsample_to_hourly(node_history_24h, zone_type=zone_type),
        })
        node_statuses.append({"node": node_name, "status": "active"})

    # caught by get_zone_data, which falls the zone back to open-meteo.
    if not valid_readings:
        raise ValueError(f"All {len(nodes)} sensor nodes offline or showing spikes")

    database.save_readings([
        {
            "zone_id": f"{zone_id}_{r['node_name']}",
            "pm2_5": r["pm2_5"],
            "pm10": r["pm10"],
            "temp": r["temp"],
            "humidity": r["humidity"],
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
                            fetched_data = await fetch_airgradient_common(
                                zone_id=zone_id,
                                loc_id=config["location_id"],
                                token=token,
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

                    except Exception as e:
                        print(f"Sensor offline for {zone_id}: {e}, falling back to Open-Meteo")
                        fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                        source_name = "openmeteo air pollution api"
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

        if pm25_val > SPIKE_PM25_CEILING or pm10_val > SPIKE_PM10_CEILING:
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
                if (current_aqi - val_1h) > WARN_AQI_JUMP:
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

        full_payload = {
            "zone_id": zone_id,
            "zone_name": zone_name,
            "source": source_name,
            "timestamp_unix": current_time,
            "coordinates": {"lat": lat, "lon": lon},
            "averages_24h": averages_24h,
            "history": history,
            "warning": warning_msg,
            "nodes": raw_comps.get("nodes"),
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
