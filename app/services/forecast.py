# SPDX-License-Identifier: MIT
#
# Copyright (C) 2026 The Breathe Open Source Project
# Copyright (C) 2026 sidharthify <wednisegit@gmail.com>
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

# short range particulate forecast for the jammu and srinagar zone averages.

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.core import database
from app.core.config import ZONES
from app.core.conversions import calculate_overall_aqi

STEP_SECONDS = 900
DAY_STEPS = 96
HISTORY_HOURS = 26
MAX_AGE_SECONDS = 3600
MODEL = "insights-blend-1"

HORIZONS_MINUTES = [15, 30, 60, 180, 360, 720, 1440]

LATEST_WEIGHTS = {
    "jammu_city": {
        "pm2_5": {15: 1.0, 30: 0.95, 60: 0.90, 180: 0.60, 360: 0.45, 720: 0.45, 1440: 1.0},
        "pm10": {15: 1.0, 30: 0.95, 60: 0.90, 180: 0.65, 360: 0.45, 720: 0.45, 1440: 1.0},
    },
    "srinagar": {
        "pm2_5": {15: 1.0, 30: 0.95, 60: 0.90, 180: 0.60, 360: 0.40, 720: 0.30, 1440: 1.0},
        "pm10": {15: 1.0, 30: 0.95, 60: 0.90, 180: 0.65, 360: 0.40, 720: 0.30, 1440: 1.0},
    },
}

FORECAST_ZONES = set(LATEST_WEIGHTS)


def _bucket_means(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, float]]:
    """Average readings onto the 15 minute grid, keyed by bucket number."""
    sums: Dict[int, Dict[str, List[float]]] = {}

    for row in rows:
        bucket = int(row["ts"] // STEP_SECONDS)
        slot = sums.setdefault(bucket, {"pm2_5": [], "pm10": []})

        for key in ("pm2_5", "pm10"):
            value = row.get(key)
            if value is not None:
                slot[key].append(float(value))

    means = {}
    for bucket, slot in sums.items():
        if not slot["pm2_5"] or not slot["pm10"]:
            continue

        means[bucket] = {
            "pm2_5": sum(slot["pm2_5"]) / len(slot["pm2_5"]),
            "pm10": sum(slot["pm10"]) / len(slot["pm10"]),
        }

    return means


def forecast_from_history(
    zone_id: str,
    rows: List[Dict[str, Any]],
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Build the forecast from a zone's recent readings, or None when they are too old."""
    if now is None:
        now = time.time()

    buckets = _bucket_means(rows)
    if not buckets:
        return None

    latest = max(buckets)
    issued_at = latest * STEP_SECONDS

    if now - issued_at > MAX_AGE_SECONDS:
        return None

    weights = LATEST_WEIGHTS[zone_id]
    zone_type = ZONES.get(zone_id, {}).get("zone_type", "hills")

    points = []
    for horizon in HORIZONS_MINUTES:
        steps = horizon // 15
        day_before = buckets.get(latest + steps - DAY_STEPS)

        values = {}
        for pollutant in ("pm2_5", "pm10"):
            current = buckets[latest][pollutant]

            if day_before is None:
                values[pollutant] = current
            else:
                weight = weights[pollutant][horizon]
                values[pollutant] = weight * current + (1.0 - weight) * day_before[pollutant]

        index = calculate_overall_aqi(values, zone_type=zone_type)

        points.append({
            "horizon_minutes": horizon,
            "ts": issued_at + horizon * 60,
            "pm2_5": round(values["pm2_5"], 2),
            "pm10": round(values["pm10"], 2),
            "us_aqi": index["us_aqi"],
            "aqi": index["aqi"],
            "uses_day_before": day_before is not None,
        })

    return {
        "zone_id": zone_id,
        "zone_name": ZONES.get(zone_id, {}).get("name", zone_id),
        "model": MODEL,
        "experimental": True,
        "issued_at": issued_at,
        "current": {
            "pm2_5": round(buckets[latest]["pm2_5"], 2),
            "pm10": round(buckets[latest]["pm10"], 2),
        },
        "points": points,
    }


async def get_zone_forecast(zone_id: str) -> Optional[Dict[str, Any]]:
    """Forecast for one of the forecast zones from its stored zone averages."""
    rows = await asyncio.to_thread(database.get_history, zone_id, HISTORY_HOURS)
    return forecast_from_history(zone_id, rows)
