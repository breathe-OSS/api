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

from app.core.config import ZONES
from app.services.fetchers import get_zone_data

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
