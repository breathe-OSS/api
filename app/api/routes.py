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
