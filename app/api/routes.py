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

# every http route the api serves.
#
# the handlers here are thin on purpose. they validate the path, look in redis,
# and hand off to app/services for anything that needs thinking about.

import asyncio
import csv
import io
import json
import re
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.config import ZONES, SENSOR_INFO
from app.core.database import VALID_METRICS, stream_historical_data, check_postgres_health
from app.core.redis_client import check_redis_health
from app.services.fetchers import get_zone_data
from app.services.seasonal import get_zone_seasonal
from app.services.weather import fetch_weather_history

# how long each kind of answer stays in redis. climatology barely moves, so it
# is held far longer than anything else.
SEASONAL_CACHE_TTL = 21600
WEATHER_CACHE_TTL = 3600
HISTORY_CACHE_TTL = 3600

DEFAULT_RANGE_SECONDS = 86400
DEFAULT_INTERVAL_SECONDS = 900

def parse_time(t_str: str) -> int:
    """Turn a duration like 1d, 6h or 1mo into seconds. Defaults to one day."""
    t_str = t_str.lower()

    # 'mo' has to be tested before 'm', or every month would be read as a
    # minute and silently return a window a thousand times too short.
    if t_str.endswith("y"):
        return int(t_str[:-1]) * 365 * 86400

    if t_str.endswith("mo"):
        return int(t_str[:-2]) * 30 * 86400

    if t_str.endswith("w"):
        return int(t_str[:-1]) * 7 * 86400

    if t_str.endswith("m"):
        return int(t_str[:-1]) * 60

    if t_str.endswith("h"):
        return int(t_str[:-1]) * 3600

    if t_str.endswith("d"):
        return int(t_str[:-1]) * 86400

    return DEFAULT_RANGE_SECONDS


async def _read_cache(cache_key: str) -> Optional[Response]:
    """Return a ready json response if this key is cached, otherwise None."""
    # imported here, not at module scope. init_redis_pool rebinds the module
    # level name at startup, so a top level "from ... import redis_client" would
    # capture None once and never see the real client.
    from app.core.redis_client import redis_client

    if not redis_client:
        return None

    cached_data = await redis_client.get(cache_key)
    if not cached_data:
        return None

    return Response(
        content=cached_data,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _write_cache(cache_key: str, payload: Any, ttl: int) -> None:
    """Store a payload under a key. A cache write failing is not an error."""
    from app.core.redis_client import redis_client

    if not redis_client:
        return

    try:
        await redis_client.set(cache_key, json.dumps(payload), ex=ttl)
    except Exception as e:
        print(f"Redis cache save error: {e}")


def register_zone_routes(app: FastAPI) -> None:
    """Attach every route to the app. Called once from main."""

    @app.get("/health")
    async def health_check():
        """Report whether the database and cache are both reachable."""
        postgres_ok, redis_ok = await asyncio.gather(
            asyncio.to_thread(check_postgres_health),
            check_redis_health(),
        )

        all_ok = postgres_ok and redis_ok

        payload = {
            "status": "ok" if all_ok else "degraded",
            "postgres": "ok" if postgres_ok else "down",
            "redis": "ok" if redis_ok else "down",
        }

        status_code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=status_code, content=payload)

    def _make_zone_handler(z: Dict[str, Any]) -> Callable[[], Any]:
        """Build one zone's handler, closing over that zone rather than the loop variable."""
        z_type = z.get("zone_type", "hills")

        async def _handler():
            """Serve this zone."""
            return await get_zone_data(z["id"], z["name"], z["lat"], z["lon"], z_type)

        return _handler

    # every zone also gets its own flat path, which is what the older app
    # releases still call.
    for zid, z in ZONES.items():
        app.get(f"/aqi/{zid}")(_make_zone_handler(z))

    @app.get("/aqi/zone/{zone_id}")
    async def get_zone_aqi(zone_id: str):
        """Current readings for one zone."""
        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")

        z = ZONES[zone_id]
        z_type = z.get("zone_type", "hills")

        return await get_zone_data(z["id"], z["name"], z["lat"], z["lon"], z_type)

    @app.get("/zones")
    async def list_zones() -> dict:
        """List every zone with enough detail to draw it on a map."""
        return {
            "zones": [
                {
                    "id": z["id"],
                    "name": z["name"],
                    "provider": z.get("provider"),
                    "lat": z.get("lat"),
                    "lon": z.get("lon"),
                    "zone_type": z.get("zone_type", "hills"),
                }
                for z in ZONES.values()
            ]
        }

    @app.get("/sensor-info")
    async def get_sensors() -> dict:
        """Hardware details for every physical sensor, straight from the json."""
        return SENSOR_INFO

    @app.get("/seasonal/{zone_id}")
    async def get_seasonal_route(zone_id: str):
        """Twelve month normals for a zone, from reanalysis."""
        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")

        cache_key = f"seasonal:{zone_id}"

        cached = await _read_cache(cache_key)
        if cached:
            return cached

        payload = await get_zone_seasonal(zone_id)
        await _write_cache(cache_key, payload, SEASONAL_CACHE_TTL)

        return payload

    @app.get("/weather-history/{zone_id}/{time_range}/{interval}")
    async def get_weather_history_route(
        zone_id: str,
        time_range: str = Path(
            ..., examples=["1w"], description="Time range (e.g., 1d, 7d, 1mo, 1y)"
        ),
        interval: str = Path(
            ..., examples=["1h"], description="Grouping interval (e.g., 1h, 4h, 1d)"
        ),
    ):
        """Past weather for a zone, bucketed to line up with a readings chart."""
        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")

        cache_key = f"weather:{zone_id}:{time_range}:{interval}"

        cached = await _read_cache(cache_key)
        if cached:
            return cached

        z = ZONES[zone_id]
        time_range_sec = parse_time(time_range)
        interval_sec = parse_time(interval)

        payload = await fetch_weather_history(z["lat"], z["lon"], time_range_sec, interval_sec)
        payload["zone_id"] = zone_id

        await _write_cache(cache_key, payload, WEATHER_CACHE_TTL)

        return payload

    @app.get("/historical-data/{location}/{time_range}/{interval}/{metrics}")
    async def get_historical_data_route(
        location: str = Path(
            ..., examples=["jammu_city"], description="The ID of the zone or 'all'"
        ),
        time_range: str = Path(
            ..., examples=["1mo"], description="Time range (e.g., 1d, 7d, 1mo, 1y)"
        ),
        interval: str = Path(
            ..., examples=["15m"], description="Grouping interval (e.g., 15m, 1h, 1d)"
        ),
        metrics: str = Path(
            ..., examples=["pm2.5,pm10"], description="Comma-separated metrics to fetch"
        ),
        format: str = Query("json", examples=["json"], description="Output format (json or csv)"),
    ):
        """Stream a window of readings as json or csv, caching the finished body."""
        cache_key = f"hist:{location}:{time_range}:{interval}:{metrics}:{format.lower()}"

        # a zone_id can contain characters that do not belong in a header value,
        # so the download name is built from a sanitised copy.
        safe_location = re.sub(r"[^A-Za-z0-9_.-]", "_", location)

        from app.core.redis_client import redis_client

        if redis_client:
            cached_data = await redis_client.get(cache_key)
            if cached_data:
                headers = {"Cache-Control": "public, max-age=3600"}

                if format.lower() == "csv":
                    headers["Content-Disposition"] = (
                        f'attachment; filename="historical_{safe_location}.csv"'
                    )
                    return Response(content=cached_data, media_type="text/csv", headers=headers)

                return Response(
                    content=cached_data, media_type="application/json", headers=headers
                )

        time_range_sec = parse_time(time_range)
        interval_sec = parse_time(interval)
        if interval_sec == 0:
            interval_sec = DEFAULT_INTERVAL_SECONDS

        metrics_list = metrics.split(",")

        actual_metrics = [VALID_METRICS[m] for m in metrics_list if m in VALID_METRICS]
        if not actual_metrics:
            actual_metrics = ["pm2_5", "pm10"]

        def generate_json():
            """Yield the json body a row at a time, tallying stats as it goes."""
            # the summary belongs at the end of the document because it can only
            # be known once every row has been seen, and the point of streaming
            # is to never hold the whole set in memory.
            yield '{"data": ['
            first = True

            max_pm25 = -1
            min_pm25 = float("inf")
            sum_pm25 = 0
            count_pm25 = 0

            max_pm10 = -1
            min_pm10 = float("inf")
            sum_pm10 = 0
            count_pm10 = 0

            for row in stream_historical_data(
                location, time_range_sec, interval_sec, metrics_list
            ):
                if not first:
                    yield ","

                yield json.dumps(row)
                first = False

                pm25 = row.get("pm2_5")
                pm10 = row.get("pm10")

                if pm25 is not None:
                    if pm25 > max_pm25:
                        max_pm25 = pm25
                    if pm25 < min_pm25:
                        min_pm25 = pm25
                    sum_pm25 += pm25
                    count_pm25 += 1

                if pm10 is not None:
                    if pm10 > max_pm10:
                        max_pm10 = pm10
                    if pm10 < min_pm10:
                        min_pm10 = pm10
                    sum_pm10 += pm10
                    count_pm10 += 1

            # the sentinels are only meaningful if something was counted, so
            # they are converted back to null where nothing was.
            stats = {}
            if count_pm25 > 0 or count_pm10 > 0:
                stats = {
                    "max_pm2_5": max_pm25 if max_pm25 >= 0 else None,
                    "min_pm2_5": min_pm25 if min_pm25 != float("inf") else None,
                    "avg_pm2_5": round(sum_pm25 / count_pm25, 2) if count_pm25 > 0 else None,
                    "max_pm10": max_pm10 if max_pm10 >= 0 else None,
                    "min_pm10": min_pm10 if min_pm10 != float("inf") else None,
                    "avg_pm10": round(sum_pm10 / count_pm10, 2) if count_pm10 > 0 else None,
                }

            yield '], "stats": ' + json.dumps(stats) + "}"

        def generate_csv():
            """Yield the csv body a row at a time."""
            # written through csv.writer rather than joining on commas, because a
            # zone_id can legitimately contain one: per sensor rows are stored as
            # "<zone>_<sensor name>", and a sensor named "Jeelanabad, Batpora"
            # would otherwise split into an extra column and break every parser
            # downstream.
            header = ["zone_id", "ts"] + actual_metrics

            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")

            def drain():
                """Take what the writer has produced and reset the buffer."""
                line = buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
                return line

            writer.writerow(header)
            yield drain()

            for row in stream_historical_data(
                location, time_range_sec, interval_sec, metrics_list
            ):
                writer.writerow([row.get(k, "") for k in header])
                yield drain()

        headers = {"Cache-Control": "public, max-age=3600"}

        # the generators below run in a worker thread, so the loop is captured
        # here while we are still on it.
        loop = asyncio.get_running_loop()

        def generate_and_cache(base_generator):
            """Pass each chunk through to the client, then cache the whole body."""
            buffer = []

            for chunk in base_generator:
                yield chunk
                buffer.append(chunk)

            if redis_client:
                full_content = "".join(buffer)

                async def save_to_redis():
                    """Store the finished body once the client has had it all."""
                    try:
                        await redis_client.set(cache_key, full_content, ex=HISTORY_CACHE_TTL)
                    except Exception as e:
                        print(f"Redis cache save error: {e}")

                # scheduled onto the loop from a thread, which is why this is
                # run_coroutine_threadsafe and not a plain create_task.
                asyncio.run_coroutine_threadsafe(save_to_redis(), loop)

        if format.lower() == "csv":
            headers["Content-Disposition"] = (
                f'attachment; filename="historical_{safe_location}.csv"'
            )
            return StreamingResponse(
                generate_and_cache(generate_csv()), media_type="text/csv", headers=headers
            )

        return StreamingResponse(
            generate_and_cache(generate_json()), media_type="application/json", headers=headers
        )
