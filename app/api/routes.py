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

from fastapi import FastAPI, HTTPException, Path, Query
from typing import Callable, Any, Dict

from app.core.config import ZONES, SENSOR_INFO
from app.services.fetchers import get_zone_data
from app.core.database import stream_historical_data
from fastapi.responses import StreamingResponse, Response
import json

def register_zone_routes(app: FastAPI) -> None:
    def _make_zone_handler(z: Dict[str, Any]) -> Callable[[], Any]:
        z_type = z.get("zone_type", "hills") 

        async def _handler():
            return await get_zone_data(
                z["id"], 
                z["name"], 
                z["lat"], 
                z["lon"],
                z_type
            )
        return _handler

    for zid, z in ZONES.items():
        path = f"/aqi/{zid}"
        handler = _make_zone_handler(z)
        app.get(path)(handler)

    @app.get("/aqi/zone/{zone_id}")
    async def get_zone_aqi(zone_id: str):
        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")
        z = ZONES[zone_id]
        z_type = z.get("zone_type", "hills")

        return await get_zone_data(
            z["id"], 
            z["name"], 
            z["lat"], 
            z["lon"], 
            z_type
        )

    @app.get("/zones")
    async def list_zones() -> dict:
        return {
            "zones": [
                {
                    "id": z["id"],
                    "name": z["name"],
                    "provider": z.get("provider"),
                    "lat": z.get("lat"),
                    "lon": z.get("lon"),
                    "zone_type": z.get("zone_type", "hills")
                }
                for z in ZONES.values()
            ]
        }

    @app.get("/sensor-info")
    async def get_sensors() -> dict:
        return SENSOR_INFO

    @app.get("/historical-data/{location}/{time_range}/{interval}/{metrics}")
    async def get_historical_data_route(
        location: str = Path(..., examples=["jammu_city"], description="The ID of the zone or 'all'"),
        time_range: str = Path(..., examples=["1mo"], description="Time range (e.g., 1d, 7d, 1mo, 1y)"),
        interval: str = Path(..., examples=["15m"], description="Grouping interval (e.g., 15m, 1h, 1d)"),
        metrics: str = Path(..., examples=["pm2.5,pm10"], description="Comma-separated metrics to fetch"),
        format: str = Query("json", examples=["json"], description="Output format (json or csv)")
    ):
        cache_key = f"hist:{location}:{time_range}:{interval}:{metrics}:{format.lower()}"
        from app.core.redis_client import redis_client
        if redis_client:
            cached_data = await redis_client.get(cache_key)
            if cached_data:
                headers = {"Cache-Control": "public, max-age=3600"}
                if format.lower() == "csv":
                    headers["Content-Disposition"] = f'attachment; filename="historical_{location}.csv"'
                    return Response(content=cached_data, media_type="text/csv", headers=headers)
                return Response(content=cached_data, media_type="application/json", headers=headers)

        def parse_time(t_str: str) -> int:
            t_str = t_str.lower()
            if t_str.endswith('y'): return int(t_str[:-1]) * 365 * 86400
            if t_str.endswith('mo'): return int(t_str[:-2]) * 30 * 86400
            if t_str.endswith('m'): return int(t_str[:-1]) * 60
            if t_str.endswith('h'): return int(t_str[:-1]) * 3600
            if t_str.endswith('d'): return int(t_str[:-1]) * 86400
            return 86400 # default 1 day

        time_range_sec = parse_time(time_range)
        interval_sec = parse_time(interval)
        if interval_sec == 0:
            interval_sec = 900 # default 15m
            
        metrics_list = metrics.split(',')
        
        valid_metrics = {'pm2.5': 'pm2_5', 'pm10': 'pm10', 'temp': 'temp', 'humidity': 'humidity'}
        actual_metrics = [valid_metrics[m] for m in metrics_list if m in valid_metrics]
        if not actual_metrics:
            actual_metrics = ['pm2_5', 'pm10']
            
        def generate_json():
            yield '{"data": ['
            first = True
            
            max_pm25 = -1
            min_pm25 = float('inf')
            sum_pm25 = 0
            count_pm25 = 0
            
            max_pm10 = -1
            min_pm10 = float('inf')
            sum_pm10 = 0
            count_pm10 = 0
            
            for row in stream_historical_data(location, time_range_sec, interval_sec, metrics_list):
                if not first:
                    yield ","
                yield json.dumps(row)
                first = False
                
                pm25 = row.get('pm2_5')
                pm10 = row.get('pm10')
                if pm25 is not None:
                    if pm25 > max_pm25: max_pm25 = pm25
                    if pm25 < min_pm25: min_pm25 = pm25
                    sum_pm25 += pm25
                    count_pm25 += 1
                    
                if pm10 is not None:
                    if pm10 > max_pm10: max_pm10 = pm10
                    if pm10 < min_pm10: min_pm10 = pm10
                    sum_pm10 += pm10
                    count_pm10 += 1
            
            stats = {}
            if count_pm25 > 0 or count_pm10 > 0:
                stats = {
                    "max_pm2_5": max_pm25 if max_pm25 >= 0 else None,
                    "min_pm2_5": min_pm25 if min_pm25 != float('inf') else None,
                    "avg_pm2_5": round(sum_pm25 / count_pm25, 2) if count_pm25 > 0 else None,
                    "max_pm10": max_pm10 if max_pm10 >= 0 else None,
                    "min_pm10": min_pm10 if min_pm10 != float('inf') else None,
                    "avg_pm10": round(sum_pm10 / count_pm10, 2) if count_pm10 > 0 else None,
                }
            yield '], "stats": ' + json.dumps(stats) + '}'
            
        def generate_csv():
            header = ["zone_id", "ts"] + actual_metrics
            yield ",".join(header) + "\n"
            for row in stream_historical_data(location, time_range_sec, interval_sec, metrics_list):
                vals = [str(row.get(k, '')) for k in header]
                yield ",".join(vals) + "\n"
                
        headers = {
            "Cache-Control": "public, max-age=3600"
        }
        
        import asyncio
        loop = asyncio.get_running_loop()

        def generate_and_cache(base_generator):
            buffer = []
            for chunk in base_generator:
                yield chunk
                buffer.append(chunk)
                        
            if redis_client:
                full_content = "".join(buffer)
                async def save_to_redis():
                    try:
                        await redis_client.set(cache_key, full_content, ex=3600)
                    except Exception as e:
                        print(f"Redis cache save error: {e}")
                asyncio.run_coroutine_threadsafe(save_to_redis(), loop)

        if format.lower() == "csv":
            headers["Content-Disposition"] = f'attachment; filename="historical_{location}.csv"'
            return StreamingResponse(generate_and_cache(generate_csv()), media_type="text/csv", headers=headers)
        
        return StreamingResponse(generate_and_cache(generate_json()), media_type="application/json", headers=headers)
