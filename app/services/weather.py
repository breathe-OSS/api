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

# weather context for a zone, from open-meteo.
#
# the point of this module is not to report the weather, it is to explain the
# air. rain scrubs particulates, fog and inversions hold them down, so knowing
# which of those is happening tells a reader why today looks the way it does.

import time
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx

from app.services.seasonal import season_for_month

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# above this, fog is reported as smog instead. fog and smog share a weather
# code, the particulate reading is the only thing that separates them.
SMOG_PM25_THRESHOLD = 55.0

# open-meteo serves about three months from the forecast endpoint. anything
# older has to come from the archive, which lags by a few days.
FORECAST_DAYS_COVERED = 90
ARCHIVE_LAG_DAYS = 6


def classify_condition(weather_code: Optional[int], pm2_5: Optional[float] = None) -> str:
    """Reduce a WMO weather code to one of the handful of conditions we show."""
    # missing code, assume the dull case rather than claiming clear skies.
    if weather_code is None:
        return "cloudy"

    if weather_code >= 95:
        return "thunderstorm"

    if weather_code in (71, 73, 75, 77, 85, 86):
        return "snow"

    if (51 <= weather_code <= 67) or (80 <= weather_code <= 82):
        return "rain"

    # 45 and 48 are fog. whether that reads as fog or as smog depends on what
    # the particulates are doing underneath it.
    if weather_code in (45, 48):
        if pm2_5 is not None and pm2_5 > SMOG_PM25_THRESHOLD:
            return "smog"
        return "fog"

    if weather_code <= 1:
        return "clear"

    return "cloudy"


def build_weather_text(condition: str, season: str) -> str:
    """One sentence on what the weather is doing to the air, for the app to show."""
    if condition == "rain":
        return (
            "Rain is washing particulates out of the air. PM2.5 typically drops "
            "during and after showers."
        )

    if condition == "thunderstorm":
        return (
            "Stormy conditions. Heavy rain scrubs PM2.5 from the air, though "
            "gusty winds can briefly kick up dust."
        )

    if condition == "snow":
        return (
            "Snowfall settles dust and particulates, but smoke from heating can "
            "keep PM2.5 elevated."
        )

    if condition == "smog":
        return (
            "Cold, stagnant air is trapping smoke and particulates near the "
            "ground. Consider limiting outdoor activity."
        )

    if condition == "fog":
        return (
            "Foggy conditions. Moisture can hold particulates near the surface "
            "until the fog lifts."
        )

    # clear skies mean something different in january than in july, so the
    # season decides which explanation is the useful one.
    if condition == "clear":
        if season == "winter":
            return (
                "Clear and cold. Overnight temperature inversions can still trap "
                "heating smoke near the ground, especially in the mornings."
            )

        if season == "monsoon":
            return (
                "Clear skies between showers. Recent rains usually keep "
                "particulate levels low this time of year."
            )

        return "Clear conditions. Pollution levels are driven mainly by local traffic and dust."

    if season == "winter":
        return (
            "Overcast and cold. Low mixing heights can hold smoke and "
            "particulates near the surface."
        )

    return "Stable conditions. Expect pollution to follow the usual daily traffic pattern."


async def get_zone_weather(
    lat: float,
    lon: float,
    pm2_5: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Current conditions for one zone. None if open-meteo does not answer."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "weather_code,precipitation",
        "timezone": "auto",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FORECAST_URL, params=params)

        if r.status_code != 200:
            print(f"Weather fetch failed: {r.status_code}")
            return None

        current = r.json().get("current", {})

    except Exception as e:
        # weather is a nice to have. a zone without it still serves its readings.
        print(f"Weather fetch error: {e}")
        return None

    code = current.get("weather_code")
    condition = classify_condition(code, pm2_5)
    season = season_for_month(datetime.now().month)

    return {
        "condition": condition,
        "weather_code": code,
        "precipitation": current.get("precipitation"),
        "season": season,
        "text": build_weather_text(condition, season),
    }


async def fetch_weather_history(
    lat: float,
    lon: float,
    time_range_sec: int,
    interval_sec: int,
) -> Dict[str, Any]:
    """Hourly weather over a window, bucketed to match a readings chart."""
    # finer than hourly is pointless, open-meteo has nothing to fill it with.
    interval_sec = max(interval_sec, 3600)

    now_ts = time.time()
    start_ts = now_ts - time_range_sec

    # two extra days of slack so the first bucket is never half empty.
    past_days = min(int(time_range_sec // 86400) + 2, 92)

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            client.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "weather_code,precipitation",
                    "past_days": past_days,
                    "forecast_days": 1,
                    "timezone": "auto",
                    "timeformat": "unixtime",
                },
            )
        ]

        # only reach for the archive when the window runs past what the forecast
        # endpoint keeps. the two overlap, and the loop below prefers the
        # forecast copy where they do.
        if time_range_sec > FORECAST_DAYS_COVERED * 86400:
            tasks.append(
                client.get(
                    ARCHIVE_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "hourly": "weather_code,precipitation",
                        "start_date": datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"),
                        "end_date": (
                            datetime.now() - timedelta(days=ARCHIVE_LAG_DAYS)
                        ).strftime("%Y-%m-%d"),
                        "timezone": "auto",
                        "timeformat": "unixtime",
                    },
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # walked in reverse so the archive lands first and the forecast overwrites
    # it on the overlapping hours, which are the fresher of the two.
    hours = {}
    for resp in reversed(results):
        if isinstance(resp, Exception) or resp.status_code != 200:
            continue

        hourly = resp.json().get("hourly", {})
        times = hourly.get("time", [])
        codes = hourly.get("weather_code", [])
        precip = hourly.get("precipitation", [])

        for i, t in enumerate(times):
            if t < start_ts or t > now_ts:
                continue

            code = codes[i] if i < len(codes) else None
            if code is None:
                continue

            hours[t] = {
                "code": code,
                "precip": precip[i] if i < len(precip) and precip[i] is not None else 0.0,
            }

    # collapse the hours into the same buckets the readings chart uses, counting
    # how many hours fell into each condition.
    buckets = {}
    for t, h in hours.items():
        bucket_ts = int(t // interval_sec) * interval_sec

        b = buckets.setdefault(bucket_ts, {"precip": 0.0, "conditions": {}})
        b["precip"] += h["precip"]

        cond = classify_condition(h["code"])
        b["conditions"][cond] = b["conditions"].get(cond, 0) + 1

    points = []
    for bucket_ts in sorted(buckets.keys()):
        b = buckets[bucket_ts]
        conds = b["conditions"]

        # a bucket is labelled by the most notable thing that happened in it,
        # not the most common. one hour of thunderstorm in a clear day is the
        # part worth showing.
        condition = None
        for priority in ("thunderstorm", "snow", "rain", "fog"):
            if conds.get(priority, 0) > 0:
                condition = priority
                break

        if condition is None:
            condition = max(conds, key=lambda k: conds[k])

        points.append({
            "ts": bucket_ts,
            "condition": condition,
            "precipitation": round(b["precip"], 1),
        })

    return {"interval": interval_sec, "points": points}
