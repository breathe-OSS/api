from fastapi import FastAPI, HTTPException
from typing import Callable, Any, Dict

from config import ZONES
from fetchers import fetch_srinagar_gov, fetch_jammu_openweather

def register_zone_routes(app: FastAPI) -> None:
    def _make_zone_handler(z: Dict[str, Any]) -> Callable[[], Any]:
        provider = z.get("provider", "")
        if provider == "cpcb_data_gov":
            async def _handler():
                return await fetch_srinagar_gov()
        else:
            async def _handler():
                return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])
        return _handler

    for zid, z in ZONES.items():
        if zid == "srinagar_gov":
            path = "/aqi/srinagar"
        else:
            path = f"/aqi/{zid}"
        handler = _make_zone_handler(z)
        app.get(path)(handler)

    @app.get("/aqi/zone/{zone_id}")
    async def get_zone_aqi(zone_id: str):
        if zone_id not in ZONES:
            raise HTTPException(status_code=404, detail="zone not found")
        z = ZONES[zone_id]
        provider = z.get("provider", "")
        if provider == "cpcb_data_gov":
            return await fetch_srinagar_gov()
        return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

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
                }
                for z in ZONES.values()
            ]
        }