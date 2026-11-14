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
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.config import ZONES, SENSOR_INFO, PLACES, SENSOR_ZONES
from app.core.conversions import calculate_overall_aqi
from app.core.database import VALID_METRICS, stream_historical_data, check_postgres_health
from app.core.redis_client import check_redis_health
from app.services.fetchers import get_zone_data, get_place_data
from app.services.seasonal import get_zone_seasonal
from app.services.weather import fetch_weather_history

# how long each kind of answer stays in redis. climatology barely moves, so it
# is held far longer than anything else.
SEASONAL_CACHE_TTL = 21600
WEATHER_CACHE_TTL = 3600
HISTORY_CACHE_TTL = 3600

# the most buckets one page of history or weather can ask for.
PAGE_LIMIT_MAX = 10000

# metrics worked out per bucket from the particulates rather than stored.
COMPUTED_METRICS = {
    "us_aqi": ("pm2.5", "pm10"),
    "us_aqi_raw": ("pm2.5_raw", "pm10_raw"),
    "naqi": ("pm2.5", "pm10"),
    "naqi_raw": ("pm2.5_raw", "pm10_raw"),
}

# which field of calculate_overall_aqi each computed metric reads.
COMPUTED_INDEX = {
    "us_aqi": "us_aqi",
    "us_aqi_raw": "us_aqi",
    "naqi": "aqi",
    "naqi_raw": "aqi",
}

DEFAULT_RANGE_SECONDS = 86400
DEFAULT_INTERVAL_SECONDS = 900

# calendar dates are read as local time for the network, which all sits in IST.
NETWORK_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
MIN_WINDOW_YEAR = 2000

MONTH_TOKEN = re.compile(r"(\d{4})-(\d{2})")
WEEK_TOKEN = re.compile(r"(\d{4})-W(\d{2})")
DAY_TOKEN = re.compile(r"\d{4}-\d{2}-\d{2}")
SPAN_TOKEN = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")

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


def _calendar_days(token: str) -> Tuple[date, date]:
    """Resolve a calendar token to its first day and the day after its last."""
    month = MONTH_TOKEN.fullmatch(token)
    if month:
        first = date(int(month.group(1)), int(month.group(2)), 1)
        return first, (first + timedelta(days=32)).replace(day=1)

    week = WEEK_TOKEN.fullmatch(token)
    if week:
        first = date.fromisocalendar(int(week.group(1)), int(week.group(2)), 1)
        return first, first + timedelta(days=7)

    if DAY_TOKEN.fullmatch(token):
        first = date.fromisoformat(token)
        return first, first + timedelta(days=1)

    span = SPAN_TOKEN.fullmatch(token)
    if span:
        first = date.fromisoformat(span.group(1))
        last = date.fromisoformat(span.group(2))
        if last < first:
            raise ValueError("span ends before it starts")
        return first, last + timedelta(days=1)

    raise ValueError("unknown calendar token")


def parse_window(t_str: str) -> Optional[Tuple[int, int]]:
    """Turn a calendar token into unix start and exclusive end, or None for a rolling duration.

    Accepts 2026-10 (month), 2026-W42 (ISO week), 2026-10-15 (day) and
    2026-10-01_2026-10-07 (inclusive span of days).
    """
    if "-" not in t_str:
        return None

    try:
        first, after_last = _calendar_days(t_str.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="time_range must look like 2026-10, 2026-W42, 2026-10-15 or "
            "2026-10-01_2026-10-07",
        )

    if first.year < MIN_WINDOW_YEAR:
        raise HTTPException(status_code=400, detail="time_range is too far in the past")

    start = datetime.combine(first, time.min, NETWORK_TIMEZONE)
    end = datetime.combine(after_last, time.min, NETWORK_TIMEZONE)

    return int(start.timestamp()), int(end.timestamp())


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


def _place_summary(place: Dict[str, Any]) -> Dict[str, Any]:
    """What /zones lists about a sensor or an estimate zone."""
    return {
        "id": place["id"],
        "name": place["name"],
        "kind": place["kind"],
        "provider": place.get("provider"),
        "lat": place.get("lat"),
        "lon": place.get("lon"),
        "zone_type": place.get("zone_type", "hills"),
        "district": place.get("district"),
        "division": place.get("division"),
        "legacy_zone": place.get("legacy_zone", place["id"]),
    }


def _bucket_index(pm2_5: Optional[float], pm10: Optional[float], index: str) -> Optional[int]:
    """The US AQI or NAQI of one bucket's mean concentrations."""
    if pm2_5 is None and pm10 is None:
        return None

    comps = {}
    if pm2_5 is not None:
        comps["pm2_5"] = pm2_5
    if pm10 is not None:
        comps["pm10"] = pm10

    return calculate_overall_aqi(comps).get(index)


def _page(items: Iterable[Dict[str, Any]], limit: int, cursor: Optional[int]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Up to limit items after the cursor, and the cursor for the page after, or None on the last one."""
    page = []
    more = False

    for item in items:
        if cursor is not None and item["ts"] <= cursor:
            continue

        if len(page) == limit:
            more = True
            break

        page.append(item)

    next_cursor = page[-1]["ts"] if more and page else None
    return page, next_cursor


def _new_tallies(keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """Empty running max, min and mean for each metric."""
    return {key: {"max": None, "min": None, "sum": 0.0, "count": 0} for key in keys}


def _tally(tallies: Dict[str, Dict[str, Any]], row: Dict[str, Any]) -> None:
    """Fold one row into the running stats."""
    for key, tally in tallies.items():
        value = row.get(key)
        if value is None:
            continue

        if tally["max"] is None or value > tally["max"]:
            tally["max"] = value
        if tally["min"] is None or value < tally["min"]:
            tally["min"] = value
        tally["sum"] += value
        tally["count"] += 1


def _stats(tallies: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The stats object for a response, empty if no row had any of the metrics."""
    stats = {}
    if any(tally["count"] > 0 for tally in tallies.values()):
        for key, tally in tallies.items():
            stats[f"max_{key}"] = tally["max"]
            stats[f"min_{key}"] = tally["min"]
            if tally["count"] > 0:
                stats[f"avg_{key}"] = round(tally["sum"] / tally["count"], 2)
            else:
                stats[f"avg_{key}"] = None
    return stats


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

    def _make_sensor_handler(sensor_id: str) -> Callable[[], Any]:
        """Build one sensor's flat path handler."""

        async def _handler():
            """Serve this sensor."""
            return await get_place_data(sensor_id)

        return _handler

    for sid in SENSOR_ZONES:
        app.get(f"/aqi/{sid}")(_make_sensor_handler(sid))

    @app.get("/aqi/zone/{zone_id}")
    async def get_zone_aqi(zone_id: str):
        """Current readings for one zone, sensor or estimate."""
        if zone_id in PLACES:
            return await get_place_data(zone_id)

        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")

        z = ZONES[zone_id]
        z_type = z.get("zone_type", "hills")

        return await get_zone_data(z["id"], z["name"], z["lat"], z["lon"], z_type)

    @app.get("/aqi/zones")
    async def get_zones_aqi(
        ids: Optional[str] = Query(
            None, description="comma separated zone ids, every zone when left out"
        ),
        layout: str = Query(
            "zones",
            description="'sensors' for every sensor and estimate zone when ids is left out",
        ),
    ) -> dict:
        """Current readings for many zones in one request, same payload as /aqi/zone/{zone_id}.

        The apps used to ask for every zone one by one, about twenty five
        requests on each refresh. Zones that fail are left out of the list
        instead of failing the whole request, which is how the apps already
        treat a zone that didn't come back.
        """
        if ids:
            # dict.fromkeys drops repeats but keeps the order they were asked in
            wanted = list(dict.fromkeys(zid.strip() for zid in ids.split(",") if zid.strip()))
            unknown = [zid for zid in wanted if zid not in ZONES and zid not in PLACES]
            if unknown:
                raise HTTPException(status_code=404, detail=f"zone not found: {', '.join(unknown)}")
        elif layout == "sensors":
            wanted = list(PLACES.keys())
        else:
            wanted = list(ZONES.keys())

        # nearly always a straight ram cache hit, but a cold cache would
        # otherwise fire every upstream call at once, and open-meteo rate limits.
        # same limit as the background updater.
        semaphore = asyncio.Semaphore(5)

        async def fetch(zone_id: str):
            """Serve one zone, or None if it fails."""
            async with semaphore:
                try:
                    if zone_id in PLACES:
                        return await get_place_data(zone_id)

                    z = ZONES[zone_id]
                    return await get_zone_data(
                        z["id"], z["name"], z["lat"], z["lon"], z.get("zone_type", "hills")
                    )
                except Exception as e:
                    print(f"Bulk AQI failed for {zone_id}: {e}")
                    return None

        results = await asyncio.gather(*(fetch(zid) for zid in wanted))
        return {"zones": [r for r in results if r is not None]}

    @app.get("/zones")
    async def list_zones(
        layout: str = Query(
            "zones",
            description="'sensors' lists every sensor and every estimate zone, with "
            "district and division",
        ),
    ) -> dict:
        """List every zone with enough detail to draw it on a map."""
        if layout == "sensors":
            return {"zones": [_place_summary(place) for place in PLACES.values()]}

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
        if zone_id in SENSOR_ZONES:
            zone_id = SENSOR_ZONES[zone_id]["legacy_zone"]

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
            ...,
            examples=["1w"],
            description="Rolling range (1d, 7d, 1mo, 1y) or calendar window in IST "
            "(2026-10, 2026-W42, 2026-10-15, 2026-10-01_2026-10-07)",
        ),
        interval: str = Path(
            ..., examples=["1h"], description="Grouping interval (e.g., 1h, 4h, 1d)"
        ),
        limit: Optional[int] = Query(
            None, ge=1, le=PAGE_LIMIT_MAX, description="Return at most this many points per page"
        ),
        cursor: Optional[int] = Query(
            None, ge=0, description="The next_cursor from the previous page"
        ),
    ):
        """Past weather for a zone, bucketed to line up with a readings chart."""
        if zone_id not in ZONES and zone_id not in SENSOR_ZONES:
            raise HTTPException(status_code=404, detail="zone not found")

        window = parse_window(time_range)

        cache_key = f"weather:{zone_id}:{time_range}:{interval}"

        cached = await _read_cache(cache_key)
        if cached and limit is None:
            return cached

        if cached:
            payload = json.loads(cached.body)
        else:
            z = SENSOR_ZONES.get(zone_id) or ZONES[zone_id]
            time_range_sec = parse_time(time_range) if window is None else 0
            interval_sec = parse_time(interval)

            payload = await fetch_weather_history(
                z["lat"], z["lon"], time_range_sec, interval_sec, window
            )
            payload["zone_id"] = zone_id

            await _write_cache(cache_key, payload, WEATHER_CACHE_TTL)

        if limit is None:
            return payload

        page, next_cursor = _page(payload.get("points", []), limit, cursor)
        return {**payload, "points": page, "next_cursor": next_cursor}

    @app.get("/historical-data/{location}/{time_range}/{interval}/{metrics}")
    async def get_historical_data_route(
        location: str = Path(
            ..., examples=["jammu_city"], description="The ID of the zone or 'all'"
        ),
        time_range: str = Path(
            ...,
            examples=["1mo"],
            description="Rolling range (1d, 7d, 1mo, 1y) or calendar window in IST "
            "(2026-10, 2026-W42, 2026-10-15, 2026-10-01_2026-10-07)",
        ),
        interval: str = Path(
            ..., examples=["15m"], description="Grouping interval (e.g., 15m, 1h, 1d)"
        ),
        metrics: str = Path(
            ..., examples=["pm2.5,pm10"], description="Comma-separated metrics to fetch"
        ),
        format: str = Query("json", examples=["json"], description="Output format (json or csv)"),
        limit: Optional[int] = Query(
            None, ge=1, le=PAGE_LIMIT_MAX, description="Return at most this many buckets per page"
        ),
        cursor: Optional[int] = Query(
            None, ge=0, description="The next_cursor from the previous page"
        ),
    ):
        """Stream a window of readings as json or csv, caching the finished body.

        With limit, one page is returned instead of the whole window, along with
        next_cursor for the page after it.
        """
        window = parse_window(time_range)

        cache_key = f"hist:{location}:{time_range}:{interval}:{metrics}:{format.lower()}"

        # a zone_id can contain characters that do not belong in a header value,
        # so the download name is built from a sanitised copy.
        safe_location = re.sub(r"[^A-Za-z0-9_.-]", "_", location)

        from app.core.redis_client import redis_client

        if redis_client and limit is None:
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

        time_range_sec = parse_time(time_range) if window is None else 0
        interval_sec = parse_time(interval)
        if interval_sec == 0:
            interval_sec = DEFAULT_INTERVAL_SECONDS

        requested = [m.strip() for m in metrics.split(",") if m.strip()]
        column_metrics = [m for m in requested if m in VALID_METRICS]
        computed = [m for m in requested if m in COMPUTED_METRICS]

        if not column_metrics and not computed:
            column_metrics = ["pm2.5", "pm10"]

        metrics_list = list(column_metrics)
        for name in computed:
            for source in COMPUTED_METRICS[name]:
                if source not in metrics_list:
                    metrics_list.append(source)

        actual_metrics = [VALID_METRICS[m] for m in column_metrics] + computed

        store_key = location
        if location in SENSOR_ZONES:
            store_key = SENSOR_ZONES[location]["history_key"]

        def rows():
            """Stored buckets with the computed metrics added and helper columns dropped."""
            for row in stream_historical_data(
                store_key, time_range_sec, interval_sec, metrics_list, window
            ):
                out = {"zone_id": location if store_key != location else row["zone_id"]}
                out["ts"] = row["ts"]

                for name in computed:
                    pm2_5_col, pm10_col = (VALID_METRICS[s] for s in COMPUTED_METRICS[name])
                    value = _bucket_index(row.get(pm2_5_col), row.get(pm10_col), COMPUTED_INDEX[name])
                    if value is not None:
                        row[name] = value

                for key in actual_metrics:
                    if row.get(key) is not None:
                        out[key] = row[key]

                yield out

        stat_keys = ["pm2_5", "pm10"] + actual_metrics

        def generate_json():
            """Yield the json body a row at a time, tallying stats as it goes."""
            # the summary belongs at the end of the document because it can only
            # be known once every row has been seen, and the point of streaming
            # is to never hold the whole set in memory.
            yield '{"data": ['
            first = True

            tallies = _new_tallies(stat_keys)

            for row in rows():
                if not first:
                    yield ","

                yield json.dumps(row)
                first = False
                _tally(tallies, row)

            yield '], "stats": ' + json.dumps(_stats(tallies)) + "}"

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

            for row in rows():
                writer.writerow([row.get(k, "") for k in header])
                yield drain()

        headers = {"Cache-Control": "public, max-age=3600"}

        if limit is not None:
            # a page is at most limit buckets, so it is built whole. that lets the
            # csv carry next_cursor in a header, which a stream can't once the
            # body has started.
            source = rows()
            page, next_cursor = await asyncio.to_thread(_page, source, limit, cursor)
            source.close()

            if format.lower() == "csv":
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                header = ["zone_id", "ts"] + actual_metrics
                writer.writerow(header)
                for row in page:
                    writer.writerow([row.get(k, "") for k in header])

                headers["Content-Disposition"] = (
                    f'attachment; filename="historical_{safe_location}.csv"'
                )
                if next_cursor is not None:
                    headers["X-Next-Cursor"] = str(next_cursor)
                return Response(content=buffer.getvalue(), media_type="text/csv", headers=headers)

            tallies = _new_tallies(stat_keys)
            for row in page:
                _tally(tallies, row)

            return JSONResponse(
                content={"data": page, "stats": _stats(tallies), "next_cursor": next_cursor},
                headers=headers,
            )

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
