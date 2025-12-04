import httpx
import asyncio
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, Any

from config import ZONES, SRINAGAR_OPENAQ_CONFIG, openaq_api_key
from conversions import calculate_overall_aqi

_RAM_CACHE = {}
CACHE_DURATION = 900  # 15 minutes

async def fetch_openaq_srinagar() -> Dict[str, Any]:
    if not openaq_api_key:
        print("WARNING: OPENAQ_API_KEY not set. Falling back to OpenMeteo logic not possible here.")
        raise HTTPException(status_code=500, detail="Server config error: Missing OpenAQ Key")

    loc_id = SRINAGAR_OPENAQ_CONFIG["location_id"]
    url = f"https://api.openaq.org/v3/locations/{loc_id}/sensors"
    headers = {"X-API-Key": openaq_api_key}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
        
        if r.status_code != 200:
            print(f"OpenAQ Error: {r.text}")
            raise HTTPException(status_code=502, detail="OpenAQ fetch failed")

        data = r.json()
        results = data.get("results", [])
        
        current_comps = {}
        sensor_map = SRINAGAR_OPENAQ_CONFIG["sensor_map"]

        for sensor in results:
            s_id = sensor.get("id")
            latest_obj = sensor.get("latest", {})
            val = latest_obj.get("value")

            if s_id in sensor_map and val is not None:
                name = sensor_map[s_id]
                current_comps[name] = val

            try:
                aqi_res = calculate_overall_aqi(hour_comps, zone_type=zone_type)
                history.append({
                    "ts": times[i],
                    "aqi": aqi_res["aqi"]
                })
            except:
                continue

        return {
            "current_comps": current_comps,
            "history": [] 
        }

async def fetch_openmeteo_live(lat: float, lon: float, zone_type: str) -> Dict[str, Any]:
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm10,pm2_5,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,ozone",
        "timezone": "auto",
        "timeformat": "unixtime",
        "past_days": 1
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="openmeteo request failed")

        data = r.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        if not times:
            raise HTTPException(status_code=404, detail="no openmeteo aq data found")

        now_ts = datetime.now().timestamp()
        closest_ts = min(times, key=lambda t: abs(t - now_ts))
        target_idx = times.index(closest_ts)

        current_comps = {
            "pm10": hourly.get("pm10", [])[target_idx],
            "pm2_5": hourly.get("pm2_5", [])[target_idx],
            "no2": hourly.get("nitrogen_dioxide", [])[target_idx],
            "so2": hourly.get("sulphur_dioxide", [])[target_idx],
            "co": hourly.get("carbon_monoxide", [])[target_idx],
            "o3": hourly.get("ozone", [])[target_idx]
        }

        current_comps = {k: v for k, v in current_comps.items() if v is not None}

        start_idx = max(0, target_idx - 24)
        history = []

        for i in range(start_idx, target_idx + 1):
            hour_comps = {
                "pm10": hourly.get("pm10", [])[i],
                "pm2_5": hourly.get("pm2_5", [])[i],
                "no2": hourly.get("nitrogen_dioxide", [])[i],
                "so2": hourly.get("sulphur_dioxide", [])[i],
                "co": hourly.get("carbon_monoxide", [])[i],
                "o3": hourly.get("ozone", [])[i]
            }
            hour_comps = {k: v for k, v in hour_comps.items() if v is not None}
            
            try:
                aqi_res = calculate_overall_aqi(hour_comps, zone_type=zone_type)
                history.append({
                    "ts": times[i],
                    "aqi": aqi_res["aqi"]
                })
            except:
                continue

        return {
            "current_comps": current_comps,
            "history": history
        }

async def get_zone_data(zone_id: str, zone_name: str, lat: float, lon: float, zone_type: str, force_refresh: bool = False):
    cached_data = _RAM_CACHE.get(zone_id)
    current_time = datetime.now().timestamp()

    if cached_data and not force_refresh:
        last_fetched = cached_data.get("timestamp_unix", 0)
        if current_time - last_fetched < CACHE_DURATION:
            return cached_data

    try:
        if zone_id == "srinagar":
            fetched_data = await fetch_openaq_srinagar()
            source_name = "openaq (official cpcb)"
        else:
            fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
            source_name = "openmeteo air pollution api"
        
        raw_comps = fetched_data["current_comps"]
        history = fetched_data["history"]
        
        aqi_data = calculate_overall_aqi(raw_comps, zone_type=zone_type)
        
        full_payload = {
            "zone_id": zone_id,
            "zone_name": zone_name,
            "source": source_name,
            "timestamp_unix": current_time,
            "coordinates": {"lat": lat, "lon": lon},
            "history": history,
            **aqi_data
        }

        _RAM_CACHE[zone_id] = full_payload
        return full_payload
        
    except Exception as e:
        print(f"Live fetch failed for {zone_id}: {e}")
        if cached_data:
            return cached_data
        raise e

async def start_background_loop():
    print("--- Background Scheduler Started ---")
    while True:
        try:
            await update_all_zones_background()
        except Exception as e:
            print(f"Error in background loop: {e}")

        await asyncio.sleep(CACHE_DURATION)

async def update_all_zones_background():
    print(f"--- Updating Zones at {datetime.now()} ---")
    for zone_id, z in ZONES.items():
        try:
            await get_zone_data(
                z["id"], 
                z["name"], 
                z["lat"], 
                z["lon"], 
                z.get("zone_type", "hills"),
                force_refresh=True 
            )
            print(f"Updated: {zone_id}")
            await asyncio.sleep(1) 
        except Exception as e:
            print(f"Failed to update {zone_id}: {e}")
    print("--- Update Cycle Complete ---")