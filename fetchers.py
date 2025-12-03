import httpx
import asyncio
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, Any

from config import data_gov_api_key, ZONES
from conversions import calculate_overall_aqi

_RAM_CACHE = {}

async def fetch_srinagar_gov() -> Dict[str, Any]:
    if not data_gov_api_key:
        raise HTTPException(status_code=500, detail="DATA_GOV_API_KEY not configured")

    resource_id = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
    params = {
        "api-key": data_gov_api_key,
        "format": "json",
        "limit": 100,
        "filters[city]": "Srinagar",
    }

    url = f"https://api.data.gov.in/resource/{resource_id}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="data.gov.in request failed")

    data = r.json()
    records = data.get("records", [])
    if not records:
        raise HTTPException(status_code=404, detail="no cpcb data for srinagar found")

    raw_pollutants = {}
    last_update = None
    lat = None
    lon = None

    for rec in records:
        p_id = rec.get("pollutant_id")
        avg_value = rec.get("avg_value")

        if p_id and avg_value not in (None, "NA"):
            try:
                raw_pollutants[p_id] = float(avg_value)
            except ValueError:
                pass

        last_update = rec.get("last_update", last_update)
        lat = float(rec.get("latitude")) if rec.get("latitude") else lat
        lon = float(rec.get("longitude")) if rec.get("longitude") else lon

    aqi_data = calculate_overall_aqi(raw_pollutants, zone_type="urban")

    return {
        "zone_id": "srinagar_gov",
        "zone_name": "Srinagar",
        "source": "CPCB (data.gov.in)",
        "last_update": last_update,
        "coordinates": {"lat": lat, "lon": lon},
        **aqi_data
    }

async def fetch_openmeteo_live(lat: float, lon: float) -> Dict[str, float]:
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm10,pm2_5,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,ozone",
        "timezone": "auto",
        "past_days": 1 
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        
        if r.status_code != 200:
            print(f"OpenMeteo Error: {r.text}")
            raise HTTPException(status_code=502, detail="openmeteo request failed")

        data = r.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        
        if not times:
            raise HTTPException(status_code=404, detail="no openmeteo aq data found")

        current_iso = datetime.now().strftime("%Y-%m-%dT%H:00")
        target_idx = -1
        
        if current_iso in times:
            target_idx = times.index(current_iso)

        raw_comps = {
            "pm10": hourly.get("pm10", [])[target_idx],
            "pm2_5": hourly.get("pm2_5", [])[target_idx],
            "no2": hourly.get("nitrogen_dioxide", [])[target_idx],
            "so2": hourly.get("sulphur_dioxide", [])[target_idx],
            "co": hourly.get("carbon_monoxide", [])[target_idx],
            "o3": hourly.get("ozone", [])[target_idx]
        }

        return {k: v for k, v in raw_comps.items() if v is not None}

async def get_zone_data(zone_id: str, zone_name: str, lat: float, lon: float, zone_type: str):
    cached_data = _RAM_CACHE.get(zone_id)

    needs_update = True
    if cached_data:
        last_fetched = cached_data.get("timestamp_unix", 0)
        if datetime.now().timestamp() - last_fetched < 900:
            needs_update = False
            return cached_data

    try:
        raw_comps = await fetch_openmeteo_live(lat, lon)
        dt = datetime.now().timestamp()
        
        aqi_data = calculate_overall_aqi(raw_comps, zone_type=zone_type)
        
        full_payload = {
            "zone_id": zone_id,
            "zone_name": zone_name,
            "source": "openmeteo air pollution api",
            "timestamp_unix": dt,
            "coordinates": {"lat": lat, "lon": lon},
            **aqi_data
        }

        _RAM_CACHE[zone_id] = full_payload
        
        return full_payload
        
    except Exception as e:
        print(f"Live fetch failed for {zone_id}: {e}")
        if cached_data:
            return cached_data
        raise e

async def update_all_zones_background():
    print("--- Background Update Started ---")
    for zone_id, z in ZONES.items():
        if z.get("provider") == "openmeteo":
            try:
                await get_zone_data(
                    z["id"], 
                    z["name"], 
                    z["lat"], 
                    z["lon"], 
                    z.get("zone_type", "hills")
                )
                print(f"Updated: {zone_id}")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Failed to update {zone_id}: {e}")
    print("--- Background Update Complete ---")