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
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core import database
from app.core.config import ZONES
from app.core.conversions import calculate_overall_aqi, get_us_aqi

STEP_SECONDS = 900
DAY_STEPS = 96
HISTORY_HOURS = 26
MAX_AGE_SECONDS = 3600
MODEL = "insights-blend-1"

HORIZONS_MINUTES = [15, 30, 60, 180, 360, 720, 1440]
GRADED_HORIZONS = [60, 180, 360, 720]
RECENT_HOURS = 12
US_CATEGORY_TOPS = [50, 100, 150, 200, 300]
IST = timezone(timedelta(hours=5, minutes=30))

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

Buckets = Dict[int, Dict[str, float]]


def _bucket_means(rows: List[Dict[str, Any]]) -> Buckets:
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


def _blend(zone_id: str, buckets: Buckets, issue: int, horizon: int) -> Tuple[Dict[str, float], bool]:
    """What the model says at bucket issue for horizon minutes ahead, using only buckets up to issue."""
    weights = LATEST_WEIGHTS[zone_id]
    day_before = buckets.get(issue + horizon // 15 - DAY_STEPS)

    values = {}
    for pollutant in ("pm2_5", "pm10"):
        current = buckets[issue][pollutant]

        if day_before is None:
            values[pollutant] = current
        else:
            weight = weights[pollutant][horizon]
            values[pollutant] = weight * current + (1.0 - weight) * day_before[pollutant]

    return values, day_before is not None


def us_aqi(values: Dict[str, float]) -> int:
    """US AQI from particulates alone."""
    sub = [get_us_aqi(key, values[key]) for key in ("pm2_5", "pm10")]
    sub = [s for s in sub if s is not None]

    if not sub:
        return 0

    return max(sub)


def us_category(aqi: int) -> int:
    """Index of the US AQI category, 0 for good up to 5 for hazardous."""
    return bisect_left(US_CATEGORY_TOPS, aqi)


def _recent_hours(buckets: Buckets, latest: int) -> List[Dict[str, Any]]:
    """Hourly means over the last RECENT_HOURS, oldest first."""
    start = latest - RECENT_HOURS * 4
    hours: Dict[int, List[Dict[str, float]]] = {}

    for bucket in range(start, latest + 1):
        values = buckets.get(bucket)
        if values is None:
            continue
        hours.setdefault(bucket * STEP_SECONDS // 3600, []).append(values)

    recent = []
    for hour in sorted(hours):
        group = hours[hour]
        values = {
            key: sum(v[key] for v in group) / len(group)
            for key in ("pm2_5", "pm10")
        }
        recent.append({
            "ts": hour * 3600,
            "pm2_5": round(values["pm2_5"], 2),
            "pm10": round(values["pm10"], 2),
            "us_aqi": us_aqi(values),
        })

    return recent


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

    zone_type = ZONES.get(zone_id, {}).get("zone_type", "hills")

    points = []
    for horizon in HORIZONS_MINUTES:
        values, uses_day_before = _blend(zone_id, buckets, latest, horizon)
        index = calculate_overall_aqi(values, zone_type=zone_type)

        points.append({
            "horizon_minutes": horizon,
            "ts": issued_at + horizon * 60,
            "pm2_5": round(values["pm2_5"], 2),
            "pm10": round(values["pm10"], 2),
            "us_aqi": index["us_aqi"],
            "aqi": index["aqi"],
            "uses_day_before": uses_day_before,
        })

    current = buckets[latest]

    return {
        "zone_id": zone_id,
        "zone_name": ZONES.get(zone_id, {}).get("name", zone_id),
        "model": MODEL,
        "experimental": True,
        "issued_at": issued_at,
        "current": {
            "pm2_5": round(current["pm2_5"], 2),
            "pm10": round(current["pm10"], 2),
            "us_aqi": us_aqi(current),
        },
        "recent": _recent_hours(buckets, latest),
        "points": points,
    }


def _rate(right: int, total: int) -> Optional[float]:
    """Share of right answers, or None when there was nothing to grade."""
    if total == 0:
        return None

    return round(right / total, 4)


def track_record_from_history(
    zone_id: str,
    rows: List[Dict[str, Any]],
    days: int,
) -> Optional[Dict[str, Any]]:
    """Replay the frozen model over stored readings and grade every target that has passed."""
    buckets = _bucket_means(rows)
    if not buckets:
        return None

    latest = max(buckets)
    first_target = latest - days * DAY_STEPS + 1
    series_every = 3600 * max(1, days // 7)

    tallies = {
        horizon: {"total": 0, "right": 0, "persistence_right": 0, "miss": 0.0}
        for horizon in GRADED_HORIZONS
    }
    series: Dict[str, List[Dict[str, Any]]] = {str(h): [] for h in GRADED_HORIZONS}
    daily: Dict[str, Dict[str, int]] = {}

    for horizon in GRADED_HORIZONS:
        steps = horizon // 15

        for target in range(first_target, latest + 1):
            issue = target - steps
            if issue not in buckets or target not in buckets:
                continue

            predicted, _ = _blend(zone_id, buckets, issue, horizon)
            actual = buckets[target]

            predicted_aqi = us_aqi(predicted)
            actual_aqi = us_aqi(actual)
            right = us_category(predicted_aqi) == us_category(actual_aqi)
            persistence_right = us_category(us_aqi(buckets[issue])) == us_category(actual_aqi)

            tally = tallies[horizon]
            tally["total"] += 1
            tally["right"] += int(right)
            tally["persistence_right"] += int(persistence_right)
            tally["miss"] += abs(predicted["pm2_5"] - actual["pm2_5"])

            target_ts = target * STEP_SECONDS
            day = datetime.fromtimestamp(target_ts, IST).date().isoformat()
            day_tally = daily.setdefault(day, {"total": 0, "right": 0})
            day_tally["total"] += 1
            day_tally["right"] += int(right)

            if target_ts % series_every == 0:
                series[str(horizon)].append({
                    "ts": target_ts,
                    "forecast_pm2_5": round(predicted["pm2_5"], 2),
                    "actual_pm2_5": round(actual["pm2_5"], 2),
                    "forecast_us_aqi": predicted_aqi,
                    "actual_us_aqi": actual_aqi,
                    "right": right,
                })

    horizons = []
    total = 0
    right = 0
    persistence_right = 0

    for horizon in GRADED_HORIZONS:
        tally = tallies[horizon]
        total += tally["total"]
        right += tally["right"]
        persistence_right += tally["persistence_right"]

        mean_miss = None
        if tally["total"]:
            mean_miss = round(tally["miss"] / tally["total"], 2)

        horizons.append({
            "horizon_minutes": horizon,
            "forecasts": tally["total"],
            "right": tally["right"],
            "right_rate": _rate(tally["right"], tally["total"]),
            "persistence_right_rate": _rate(tally["persistence_right"], tally["total"]),
            "mean_miss_pm2_5": mean_miss,
        })

    return {
        "zone_id": zone_id,
        "zone_name": ZONES.get(zone_id, {}).get("name", zone_id),
        "model": MODEL,
        "days": days,
        "rule": "same US AQI category",
        "from": first_target * STEP_SECONDS,
        "to": latest * STEP_SECONDS,
        "overall": {
            "forecasts": total,
            "right": right,
            "right_rate": _rate(right, total),
            "persistence_right_rate": _rate(persistence_right, total),
        },
        "horizons": horizons,
        "daily": [
            {
                "date": day,
                "forecasts": daily[day]["total"],
                "right": daily[day]["right"],
                "right_rate": _rate(daily[day]["right"], daily[day]["total"]),
            }
            for day in sorted(daily)
        ],
        "series": series,
    }


async def get_zone_forecast(zone_id: str) -> Optional[Dict[str, Any]]:
    """Forecast for one of the forecast zones from its stored zone averages."""
    rows = await asyncio.to_thread(database.get_history, zone_id, HISTORY_HOURS)
    return forecast_from_history(zone_id, rows)


async def get_zone_track_record(zone_id: str, days: int) -> Optional[Dict[str, Any]]:
    """How the forecast would have done over the last so many days of stored readings."""
    hours = days * 24 + 12 + HISTORY_HOURS
    rows = await asyncio.to_thread(database.get_history, zone_id, hours)
    return track_record_from_history(zone_id, rows, days)
