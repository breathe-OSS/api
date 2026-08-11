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

import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Dict, Any, List

from app.core.config import ZONES
from app.core.conversions import calculate_overall_aqi
from app.core import database

BACKFILL_START = "2022-08-01"
CLIMATOLOGY_MAX_AGE = 30 * 86400
MIN_SENSOR_SAMPLES = 200

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring",
    5: "summer", 6: "summer",
    7: "monsoon", 8: "monsoon", 9: "monsoon",
    10: "autumn", 11: "autumn"
}

def season_for_month(month: int) -> str:
    return SEASONS.get(month, "summer")

async def _fetch_monthly_pollution(client: httpx.AsyncClient, lat: float, lon: float) -> Dict[int, Dict[str, List[float]]]:
    buckets = {m: {"pm2_5": [], "pm10": []} for m in range(1, 13)}

    start = datetime.strptime(BACKFILL_START, "%Y-%m-%d")
    end = datetime.now() - timedelta(days=2)

    year = start.year
    while year <= end.year:
        chunk_start = max(start, datetime(year, 1, 1))
        chunk_end = min(end, datetime(year, 12, 31))

        params = {
            "latitude": lat, "longitude": lon,
            "hourly": "pm2_5,pm10",
            "start_date": chunk_start.strftime("%Y-%m-%d"),
            "end_date": chunk_end.strftime("%Y-%m-%d"),
            "timezone": "auto", "timeformat": "unixtime"
        }

        r = await client.get("https://air-quality-api.open-meteo.com/v1/air-quality", params=params)
        if r.status_code != 200:
            print(f"Climatology pollution fetch failed for {year}: {r.status_code}")
            year += 1
            continue

        hourly = r.json().get("hourly", {})
        times = hourly.get("time", [])
        pm25_vals = hourly.get("pm2_5", [])
        pm10_vals = hourly.get("pm10", [])

        for i, t in enumerate(times):
            month = datetime.fromtimestamp(t).month
            if i < len(pm25_vals) and pm25_vals[i] is not None:
                buckets[month]["pm2_5"].append(pm25_vals[i])
            if i < len(pm10_vals) and pm10_vals[i] is not None:
                buckets[month]["pm10"].append(pm10_vals[i])

        year += 1

    return buckets

async def _fetch_monthly_weather(client: httpx.AsyncClient, lat: float, lon: float) -> Dict[int, Dict[str, Any]]:
    end = datetime.now() - timedelta(days=6)

    params = {
        "latitude": lat, "longitude": lon,
        "daily": "precipitation_sum,temperature_2m_mean",
        "start_date": BACKFILL_START,
        "end_date": end.strftime("%Y-%m-%d"),
        "timezone": "auto"
    }

    r = await client.get("https://archive-api.open-meteo.com/v1/archive", params=params)
    if r.status_code != 200:
        print(f"Climatology weather fetch failed: {r.status_code}")
        return {}

    daily = r.json().get("daily", {})
    times = daily.get("time", [])
    precip_vals = daily.get("precipitation_sum", [])
    temp_vals = daily.get("temperature_2m_mean", [])

    precip_totals = {}
    temps = {m: [] for m in range(1, 13)}

    for i, day in enumerate(times):
        d = datetime.strptime(day, "%Y-%m-%d")
        key = (d.year, d.month)
        if i < len(precip_vals) and precip_vals[i] is not None:
            precip_totals[key] = precip_totals.get(key, 0.0) + precip_vals[i]
        if i < len(temp_vals) and temp_vals[i] is not None:
            temps[d.month].append(temp_vals[i])

    result = {}
    for m in range(1, 13):
        month_totals = [v for (y, mo), v in precip_totals.items() if mo == m]
        result[m] = {
            "precipitation": sum(month_totals) / len(month_totals) if month_totals else None,
            "temp": sum(temps[m]) / len(temps[m]) if temps[m] else None
        }

    return result

async def backfill_zone_climatology(zone_id: str, lat: float, lon: float):
    print(f"Backfilling seasonal climatology for {zone_id}...")

    async with httpx.AsyncClient(timeout=60) as client:
        pollution = await _fetch_monthly_pollution(client, lat, lon)
        weather = await _fetch_monthly_weather(client, lat, lon)

    now = datetime.now().timestamp()
    rows = []

    for m in range(1, 13):
        pm25_list = pollution[m]["pm2_5"]
        pm10_list = pollution[m]["pm10"]
        if not pm25_list:
            continue

        wx = weather.get(m, {})
        rows.append({
            "month": m,
            "pm2_5": sum(pm25_list) / len(pm25_list),
            "pm10": sum(pm10_list) / len(pm10_list) if pm10_list else None,
            "precipitation": wx.get("precipitation"),
            "temp": wx.get("temp"),
            "updated_at": now
        })

    database.save_seasonal_climatology(zone_id, rows)
    print(f"Saved climatology for {zone_id} ({len(rows)} months)")

async def get_zone_seasonal(zone_id: str) -> Dict[str, Any]:
    zone = ZONES[zone_id]
    zone_type = zone.get("zone_type", "hills")

    rows = database.get_seasonal_climatology(zone_id)
    if not rows:
        await backfill_zone_climatology(zone_id, zone["lat"], zone["lon"])
        rows = database.get_seasonal_climatology(zone_id)

    sensor_months = database.get_monthly_sensor_averages(zone_id)

    months = []
    for row in rows:
        m = row["month"]
        comps = {"pm2_5": row["pm2_5"]}
        if row["pm10"] is not None:
            comps["pm10"] = row["pm10"]

        try:
            aqi_res = calculate_overall_aqi(comps, zone_type=zone_type)
        except Exception:
            aqi_res = {"aqi": 0, "us_aqi": 0}

        entry = {
            "month": m,
            "name": MONTH_NAMES[m - 1],
            "season": season_for_month(m),
            "pm2_5": round(row["pm2_5"], 1),
            "pm10": round(row["pm10"], 1) if row["pm10"] is not None else None,
            "aqi": aqi_res.get("aqi", 0),
            "us_aqi": aqi_res.get("us_aqi", 0),
            "precipitation": round(row["precipitation"], 1) if row["precipitation"] is not None else None,
            "temp": round(row["temp"], 1) if row["temp"] is not None else None
        }

        sensor = sensor_months.get(m)
        if sensor and sensor["samples"] >= MIN_SENSOR_SAMPLES:
            entry["sensor_pm2_5"] = round(sensor["pm2_5"], 1)
            entry["sensor_pm10"] = round(sensor["pm10"], 1) if sensor["pm10"] is not None else None

        months.append(entry)

    current_month = datetime.now().month

    return {
        "zone_id": zone_id,
        "zone_name": zone["name"],
        "current_month": current_month,
        "current_season": season_for_month(current_month),
        "months": months,
        "source": f"openmeteo reanalysis ({BACKFILL_START[:4]}-present)"
    }

async def refresh_stale_climatology():
    semaphore = asyncio.Semaphore(2)
    now = datetime.now().timestamp()

    async def throttled_backfill(z):
        async with semaphore:
            try:
                await backfill_zone_climatology(z["id"], z["lat"], z["lon"])
            except Exception as e:
                print(f"Climatology refresh failed for {z['id']}: {e}")

    stale_zones = []
    for z in ZONES.values():
        rows = database.get_seasonal_climatology(z["id"])
        if not rows or (now - max(r["updated_at"] or 0 for r in rows)) > CLIMATOLOGY_MAX_AGE:
            stale_zones.append(z)

    if not stale_zones:
        return

    print(f"Refreshing seasonal climatology for {len(stale_zones)} zones...")
    await asyncio.gather(*[throttled_backfill(z) for z in stale_zones])
