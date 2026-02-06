import httpx
import asyncio
from datetime import datetime, timedelta
from fastapi import HTTPException
from typing import Dict, Any, List

from app.core.config import ZONES, SRINAGAR_AIRGRADIENT_CONFIG, JAMMU_AIRGRADIENT_CONFIG, RAJOURI_AIRGRADIENT_CONFIG, airgradient_token, jammu_airgradient_token
from app.core.conversions import calculate_overall_aqi
from app.core import database

_RAM_CACHE = {}
CACHE_DURATION = 900  # 15 minutes

async def fetch_airgradient_history(client: httpx.AsyncClient, loc_id: int, token: str) -> List[Dict[str, Any]]:
    # 1 day of history
    url = f"https://api.airgradient.com/public/api/v1/locations/{loc_id}/measures/past"
    
    params = {
        "token": token,
        "period": "1day"
    }

    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            print(f"AG History Failed: {r.status_code}")
            return []
            
        data = r.json() 
        history = []

        for entry in data:
            ts = entry.get("timestamp", 0)
            if ts > 9999999999: ts = ts / 1000  # Convert ms to sec
            
            pm25 = entry.get("pm02_corrected") or entry.get("pm02")
            pm10 = entry.get("pm10_corrected") or entry.get("pm10")
            
            if pm25 is not None:
                history.append({
                    "ts": ts,
                    "pm2_5": float(pm25),
                    "pm10": float(pm10) if pm10 else 0.0
                })
        return history
    except Exception as e:
        print(f"AG History Fetch Error: {e}")
        return []

async def ensure_history_exists(zone_id: str, loc_id: int, token: str):
    """Checks DB. If empty, fetches API history and refills DB."""
    existing_data = database.get_history(zone_id, hours=1)
    
    if not existing_data:
        print(f" DB empty for {zone_id}. Refilling from AirGradient API...")
        async with httpx.AsyncClient() as client:
            history = await fetch_airgradient_history(client, loc_id, token)
            count = 0
            for pt in history:
                database.save_reading(zone_id, pt["pm2_5"], pt["pm10"], timestamp=pt["ts"])
                count += 1
            print(f" Refilled {count} records for {zone_id}")

def _get_merged_history(zone_id: str, om_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    local_data = database.get_history(zone_id, hours=24)

    sensor_start_ts = None
    if local_data:
        local_data.sort(key=lambda x: x["ts"])
        # Sort to find the very first real reading
        first_real_ts = local_data[0]["ts"]
        dt = datetime.fromtimestamp(first_real_ts)
        sensor_start_ts = dt.replace(minute=0, second=0, microsecond=0).timestamp()

    history_buckets = {}

    for pt in om_points:
        ts = pt['ts']

        if sensor_start_ts and ts >= sensor_start_ts:
            continue

        if ts not in history_buckets: history_buckets[ts] = {}
        history_buckets[ts][pt['param']] = pt['val']

    if local_data:
        for pt in local_data:
            dt = datetime.fromtimestamp(pt["ts"])
            hour_ts = dt.replace(minute=0, second=0, microsecond=0).timestamp()
            
            if hour_ts not in history_buckets: history_buckets[hour_ts] = {}
            history_buckets[hour_ts]["pm2_5"] = pt["pm2_5"]
            history_buckets[hour_ts]["pm10"] = pt["pm10"]

    sorted_times = sorted(history_buckets.keys())
    now_ts = datetime.now().timestamp()

    clip_start_ts = now_ts - (24 * 3600)
    if sensor_start_ts:
         clip_start_ts = sensor_start_ts

    final_history = []
    
    for ts in sorted_times:
        if ts < clip_start_ts or ts > now_ts:
            continue 
            
        hour_comps = history_buckets[ts]
        try:
            aqi_res = calculate_overall_aqi(hour_comps, zone_type="urban")
            final_history.append({
                "ts": int(ts),
                "aqi": aqi_res["aqi"],
                "us_aqi": aqi_res.get("us_aqi", 0)
            })
        except:
            continue
            
    return final_history

async def fetch_airgradient_common(
    zone_id: str,
    loc_id: int,
    token: str,
    lat: float,
    lon: float,
    zone_type: str = "urban"
) -> Dict[str, Any]:
    """
    Common fetcher for any AirGradient zone.
    """
    if not token:
        raise HTTPException(status_code=500, detail=f"Missing AG Token for {zone_id}")

    await ensure_history_exists(zone_id, loc_id, token)

    async with httpx.AsyncClient(timeout=20) as client:
        curr_url = f"https://api.airgradient.com/public/api/v1/locations/{loc_id}/measures/current?token={token}"
        task_curr = client.get(curr_url)
        
        om_url = "https://air-quality-api.open-meteo.com/v1/air-quality"
        om_params = {
            "latitude": lat, "longitude": lon,
            "hourly": "ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide",
            "timezone": "auto", "timeformat": "unixtime", "past_days": 1
        }
        task_om = client.get(om_url, params=om_params)
        
        results = await asyncio.gather(task_curr, task_om)
    
    ag_resp = results[0]
    om_resp = results[1]
    
    current_comps = {}

    if ag_resp.status_code == 200:
        d = ag_resp.json()
        pm25 = d.get("pm02_corrected") or d.get("pm02")
        pm10 = d.get("pm10_corrected") or d.get("pm10")
        
        current_comps["pm2_5"] = pm25
        current_comps["pm10"] = pm10
        current_comps["temp"] = d.get("atmp_corrected") or d.get("atmp")
        current_comps["humidity"] = d.get("rhum_corrected") or d.get("rhum")

        ag_timestamp = d.get("timestamp")
        if ag_timestamp:
            # API returns ISO format
            try:
                dt = datetime.fromisoformat(ag_timestamp.replace("Z", "+00:00"))
                current_comps["_ag_timestamp"] = dt.timestamp()
            except:
                pass
        
        # Save reading to DB
        if pm25 is not None:
            database.save_reading(zone_id, float(pm25), float(pm10) if pm10 else 0.0)
    else:
        print(f"AG Live Fetch Failed for {zone_id}: {ag_resp.status_code}")

    om_points = []
    if om_resp.status_code == 200:
        om_json = om_resp.json()
        hourly = om_json.get("hourly", {})
        times = hourly.get("time", [])
        o3_vals = hourly.get("ozone", [])
        no2_vals = hourly.get("nitrogen_dioxide", [])
        so2_vals = hourly.get("sulphur_dioxide", [])
        co_vals = hourly.get("carbon_monoxide", [])
        
        for i, t in enumerate(times):
            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3": vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co": vals = co_vals
                    case _: vals = []
                if i < len(vals) and vals[i] is not None:
                    om_points.append({"ts": t, "param": param, "val": vals[i]})
        
        if times:
            now_ts = datetime.now().timestamp()
            closest_ts = min(times, key=lambda t: abs(t - now_ts))
            idx = times.index(closest_ts)

            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3": vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co": vals = co_vals
                    case _: vals = []
                
                found_val = None
                for step in range(0, 6):
                    check_idx = idx - step
                    if 0 <= check_idx < len(vals) and vals[check_idx] is not None:
                        found_val = vals[check_idx]
                        break
                if found_val is not None:
                    current_comps[param] = found_val

    history = _get_merged_history(zone_id, om_points)

    return {
        "current_comps": current_comps,
        "history": history 
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

        start_ts = now_ts - (24 * 3600)
        history = []

        for i, t in enumerate(times):
            if t < start_ts or t > now_ts:
                continue

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
                    "aqi": aqi_res["aqi"],
                    "us_aqi": aqi_res.get("us_aqi", 0)
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
        sensor_offline_warning = None
        
        if zone_id in ("srinagar", "jammu_city", "rajouri_town"):
            # Try AG and fallback to Open-Meteo if sensor is down
            try:
                if zone_id == "srinagar":
                    config = SRINAGAR_AIRGRADIENT_CONFIG
                    token = airgradient_token
                elif zone_id == "jammu_city":
                    config = JAMMU_AIRGRADIENT_CONFIG
                    token = jammu_airgradient_token
                else:  # rajouri_town
                    config = RAJOURI_AIRGRADIENT_CONFIG
                    token = jammu_airgradient_token
                
                fetched_data = await fetch_airgradient_common(
                    zone_id=zone_id,
                    loc_id=config["location_id"],
                    token=token,
                    lat=lat,
                    lon=lon,
                    zone_type=zone_type
                )
                
                # Check if we got valid PM data
                pm25 = fetched_data["current_comps"].get("pm2_5")
                if pm25 is None:
                    raise ValueError("No PM2.5 data from sensor")
                
                # Older than 1 hour = sensor offline
                ag_timestamp = fetched_data["current_comps"].get("_ag_timestamp")
                if ag_timestamp:
                    data_age = current_time - ag_timestamp
                    if data_age > 3600:  # 1 hour
                        raise ValueError(f"Sensor data is stale ({int(data_age/60)} minutes old)")
                    
                source_name = "airgradient + openmeteo"
            except Exception as e:
                print(f"Sensor offline for {zone_id}: {e}, falling back to Open-Meteo")
                fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
                source_name = "openmeteo air pollution api"
                sensor_offline_warning = "Physical sensor temporarily offline. Using satellite-based estimates from Open-Meteo."
        else:
            fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
            source_name = "openmeteo air pollution api"
        
        raw_comps = fetched_data["current_comps"]
        history = fetched_data["history"]
        
        aqi_data = calculate_overall_aqi(raw_comps, zone_type=zone_type)
        current_aqi = aqi_data.get("aqi", 0)

        trend_1h = None
        trend_24h = None
        warning_msg = None
        
        WARNING_TEXT = "Warning: Unnatural spikes in sensors could be influenced by other atmospheric factors at the moment and this may not reflect the actual readings of the region"

        # Check for absolute limits (PM2.5 > 650 OR PM10 > 600)
        # Using raw_comps since it holds the direct concentration values
        pm25_val = raw_comps.get("pm2_5", 0)
        pm10_val = raw_comps.get("pm10", 0)

        if pm25_val > 650 or pm10_val > 600:
            warning_msg = WARNING_TEXT

        def get_past_aqi(target_ts, history_list, tolerance=1800):
            for point in history_list:
                if abs(point['ts'] - target_ts) <= tolerance:
                    return point['aqi']
            return None

        if history:
            ts_1h_ago = current_time - 3600
            ts_24h_ago = current_time - 86400

            val_1h = get_past_aqi(ts_1h_ago, history)
            val_24h = get_past_aqi(ts_24h_ago, history)

            if val_1h is not None:
                trend_1h = current_aqi - val_1h
                
                # Check for spikes (> 150 AQI jump in 1h)
                # Only apply if we haven't already triggered the warning from the absolute limit check
                if not warning_msg and (current_aqi - val_1h) > 150: 
                    warning_msg = WARNING_TEXT
            
            if val_24h is not None:
                trend_24h = current_aqi - val_24h

        # Merge sensor offline warning with any existing warning
        if sensor_offline_warning:
            if warning_msg:
                warning_msg = f"{sensor_offline_warning}\n\n{warning_msg}"
            else:
                warning_msg = sensor_offline_warning

        full_payload = {
            "zone_id": zone_id,
            "zone_name": zone_name,
            "source": source_name,
            "timestamp_unix": current_time,
            "coordinates": {"lat": lat, "lon": lon},
            "history": history,
            "trends": {
                "change_1h": trend_1h, 
                "change_24h": trend_24h
            },
            "warning": warning_msg, 
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
