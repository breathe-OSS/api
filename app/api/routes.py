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

from fastapi import FastAPI, HTTPException
from typing import Callable, Any, Dict

from app.core.config import ZONES, SENSOR_INFO
from app.services.fetchers import get_zone_data
from app.core.database import stream_historical_data
from fastapi.responses import StreamingResponse
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
    async def get_historical_data_route(location: str, time_range: str, interval: str, metrics: str, format: str = "json"):
        def parse_time(t_str: str) -> int:
            t_str = t_str.lower()
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
            yield "["
            first = True
            for row in stream_historical_data(location, time_range_sec, interval_sec, metrics_list):
                if not first:
                    yield ","
                yield json.dumps(row)
                first = False
            yield "]"
            
        def generate_csv():
            header = ["zone_id", "ts"] + actual_metrics
            yield ",".join(header) + "\n"
            for row in stream_historical_data(location, time_range_sec, interval_sec, metrics_list):
                vals = [str(row.get(k, '')) for k in header]
                yield ",".join(vals) + "\n"
                
        headers = {
            "Cache-Control": "public, max-age=900"
        }
        
        if format.lower() == "csv":
            headers["Content-Disposition"] = f'attachment; filename="historical_{location}.csv"'
            return StreamingResponse(generate_csv(), media_type="text/csv", headers=headers)
            
        return StreamingResponse(generate_json(), media_type="application/json", headers=headers)
