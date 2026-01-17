import httpx
import asyncio
import os
from datetime import datetime, timedelta
from fastapi import HTTPException
from typing import Dict, Any, List

from config import ZONES, SRINAGAR_AIRGRADIENT_CONFIG, JAMMU_AIRGRADIENT_CONFIG, airgradient_token, jammu_airgradient_token
from conversions import calculate_overall_aqi
import database

_RAM_CACHE = {}
CACHE_DURATION = 900 # 15 minutes

def _get_merged_history(zone_id: str, om_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    local_data = database.get_history(zone_id, hours=24)
    history_buckets = {}

    for pt in om_points:
        ts = pt['ts']
        if ts not in history_buckets: history_buckets[ts] = {}
        history_buckets[ts][pt['param']] = pt['val']

    if local_data:
        # clear estimated PM data to prioritize sensor data
        for ts in history_buckets:
            history_buckets[ts].pop("pm2_5", None)
            history_buckets[ts].pop("pm10", None)

        for pt in local_data:
            dt = datetime.fromtimestamp(pt["ts"])
            hour_ts = dt.replace(minute=0, second=0, microsecond=0).timestamp()
            
            if hour_ts not in history_buckets: history_buckets[hour_ts] = {}

            history_buckets[hour_ts]["pm2_5"] = pt["pm2_5"]
            history_buckets[hour_ts]["pm10"] = pt["pm10"]

        sorted_times = sorted(history_buckets.keys())
        
        # linear interpolation for gaps
        for param in ["pm2_5", "pm10"]:
            known = [(t, history_buckets[t][param]) for t in sorted_times if param in history_buckets[t]]
            
            if len(known) > 1:
                for i in range(len(known) - 1):
                    t1, v1 = known[i]
                    t2, v2 = known[i+1]

                    for ts in sorted_times:
                        if t1 < ts < t2:
                            fraction = (ts - t1) / (t2 - t1)
                            val = v1 + (v2 - v1) * fraction
                            history_buckets[ts][param] = val

    sorted_times = sorted(history_buckets.keys())
    now_ts = datetime.now().timestamp()
    start_ts = now_ts - (24 * 3600)
    
    final_history = []
    
    for ts in sorted_times:
        if ts < start_ts or ts > now_ts:
            continue 
            
        hour_comps = history_buckets[ts]
        try:
            aqi_res = calculate_overall_aqi(hour_comps, zone_type="urban")
            final_history.append({
                "ts": int(ts),
                "aqi": aqi_res["aqi"]
            })
        except:
            continue
            
    return final_history

async def fetch_airgradient_srinagar(lat: float, lon: float, zone_type: str = "hills") -> Dict[str, Any]:
    if not airgradient_token:
        print("WARNING: AIRGRADIENT_TOKEN not set.")
        raise HTTPException(status_code=500, detail="Server config error: Missing AirGradient Token")

    loc_id = SRINAGAR_AIRGRADIENT_CONFIG["location_id"]
    
    # OpenMeteo Params
    om_url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    om_params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide",
        "timezone": "auto",
        "timeformat": "unixtime",
        "past_days": 1
    }

    # Fetch Data
    async with httpx.AsyncClient(timeout=20) as client:
        ag_url = f"https://api.airgradient.com/public/api/v1/locations/{loc_id}/measures/current?token={airgradient_token}"
        ag_task = client.get(ag_url)
        om_task = client.get(om_url, params=om_params)
        all_results = await asyncio.gather(ag_task, om_task)

    ag_resp = all_results[0]
    om_resp = all_results[1]
    
    current_comps = {}

    if ag_resp.status_code == 200:
        d = ag_resp.json()
        pm25 = d.get("pm02_corrected") if d.get("pm02_corrected") is not None else d.get("pm02")
        pm10 = d.get("pm10_corrected") if d.get("pm10_corrected") is not None else d.get("pm10")
        
        current_comps["pm2_5"] = pm25
        current_comps["pm10"] = pm10
        current_comps["temp"] = d.get("atmp_corrected") if d.get("atmp_corrected") is not None else d.get("atmp")
        current_comps["humidity"] = d.get("rhum_corrected") if d.get("rhum_corrected") is not None else d.get("rhum")
        
        # Save to DB if valid
        if pm25 is not None and pm10 is not None:
            database.save_reading("srinagar", float(pm25), float(pm10))
    else:
        print(f"AirGradient Error: {ag_resp.text}")
        raise HTTPException(status_code=502, detail="AirGradient fetch failed")

    om_points = []
    if om_resp.status_code == 200:
        om_json = om_resp.json()
        hourly = om_json.get("hourly", {})
        times = hourly.get("time", [])
        
        o3_vals = hourly.get("ozone", [])
        no2_vals = hourly.get("nitrogen_dioxide", [])
        so2_vals = hourly.get("sulphur_dioxide", [])
        co_vals = hourly.get("carbon_monoxide", [])
        
        # Build history points
        for i, t in enumerate(times):
            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3":  vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co":  vals = co_vals
                    case _:     vals = []

                if i < len(vals) and vals[i] is not None:
                    om_points.append({"ts": t, "param": param, "val": vals[i]})

        if times:
            now_ts = datetime.now().timestamp()
            closest_ts = min(times, key=lambda t: abs(t - now_ts))
            idx = times.index(closest_ts)
            
            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3":  vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co":  vals = co_vals
                    case _:     vals = []

                found_val = None
                for step in range(0, 6):
                    check_idx = idx - step
                    if 0 <= check_idx < len(vals) and vals[check_idx] is not None:
                        found_val = vals[check_idx]
                        break
                
                if found_val is not None:
                    current_comps[param] = found_val

    current_comps = {k: v for k, v in current_comps.items() if v is not None}

    if "pm2_5" not in current_comps:
        db_history = database.get_history("srinagar", hours=2)
        if db_history:
            last_pt = db_history[-1]
            current_comps["pm2_5"] = last_pt["pm2_5"]
            current_comps["pm10"] = last_pt["pm10"]

    history = _get_merged_history("srinagar", om_points)

    return {
        "current_comps": current_comps,
        "history": history 
    }

async def fetch_airgradient_jammu(lat: float, lon: float, zone_type: str = "urban") -> Dict[str, Any]:
    if not jammu_airgradient_token:
        print("WARNING: JAMMU_AIRGRADIENT_TOKEN not set.")
        raise HTTPException(status_code=500, detail="Server config error: Missing Jammu AirGradient Token")

    loc_id = JAMMU_AIRGRADIENT_CONFIG["location_id"]

    # OpenMeteo Params
    om_url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    om_params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide",
        "timezone": "auto",
        "timeformat": "unixtime",
        "past_days": 1
    }

    # Fetch Data
    async with httpx.AsyncClient(timeout=20) as client:
        ag_url = f"https://api.airgradient.com/public/api/v1/locations/{loc_id}/measures/current?token={jammu_airgradient_token}"
        ag_task = client.get(ag_url)
        om_task = client.get(om_url, params=om_params)
        all_results = await asyncio.gather(ag_task, om_task)

    ag_resp = all_results[0]
    om_resp = all_results[1]
    
    current_comps = {}

    if ag_resp.status_code == 200:
        d = ag_resp.json()
        pm25 = d.get("pm02_corrected") if d.get("pm02_corrected") is not None else d.get("pm02")
        pm10 = d.get("pm10_corrected") if d.get("pm10_corrected") is not None else d.get("pm10")
        
        current_comps["pm2_5"] = pm25
        current_comps["pm10"] = pm10
        current_comps["temp"] = d.get("atmp_corrected") if d.get("atmp_corrected") is not None else d.get("atmp")
        current_comps["humidity"] = d.get("rhum_corrected") if d.get("rhum_corrected") is not None else d.get("rhum")

        # Save to DB if valid
        if pm25 is not None and pm10 is not None:
            database.save_reading("jammu_city", float(pm25), float(pm10))
    else:
        print(f"AirGradient Error: {ag_resp.text}")
        raise HTTPException(status_code=502, detail="AirGradient fetch failed")

    om_points = []
    if om_resp.status_code == 200:
        om_json = om_resp.json()
        hourly = om_json.get("hourly", {})
        times = hourly.get("time", [])
        
        o3_vals = hourly.get("ozone", [])
        no2_vals = hourly.get("nitrogen_dioxide", [])
        so2_vals = hourly.get("sulphur_dioxide", [])
        co_vals = hourly.get("carbon_monoxide", [])
        
        # Build history points
        for i, t in enumerate(times):
            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3":  vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co":  vals = co_vals
                    case _:     vals = []

                if i < len(vals) and vals[i] is not None:
                    om_points.append({"ts": t, "param": param, "val": vals[i]})

        if times:
            now_ts = datetime.now().timestamp()
            closest_ts = min(times, key=lambda t: abs(t - now_ts))
            idx = times.index(closest_ts)
            
            for param in ["o3", "no2", "so2", "co"]:
                match param:
                    case "o3":  vals = o3_vals
                    case "no2": vals = no2_vals
                    case "so2": vals = so2_vals
                    case "co":  vals = co_vals
                    case _:     vals = []

                found_val = None
                for step in range(0, 6):
                    check_idx = idx - step
                    if 0 <= check_idx < len(vals) and vals[check_idx] is not None:
                        found_val = vals[check_idx]
                        break
                
                if found_val is not None:
                    current_comps[param] = found_val

    current_comps = {k: v for k, v in current_comps.items() if v is not None}

    if "pm2_5" not in current_comps:
        db_history = database.get_history("jammu_city", hours=2)
        if db_history:
            last_pt = db_history[-1]
            current_comps["pm2_5"] = last_pt["pm2_5"]
            current_comps["pm10"] = last_pt["pm10"]

    history = _get_merged_history("jammu_city", om_points)

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
            fetched_data = await fetch_airgradient_srinagar(lat, lon, zone_type=zone_type)
            source_name = "airgradient + openmeteo"
        elif zone_id == "jammu_city":
            fetched_data = await fetch_airgradient_jammu(lat, lon, zone_type=zone_type)
            source_name = "airgradient + openmeteo"
        else:
            fetched_data = await fetch_openmeteo_live(lat, lon, zone_type)
            source_name = "openmeteo air pollution api"
        
        raw_comps = fetched_data["current_comps"]
        history = fetched_data["history"]
        
        aqi_data = calculate_overall_aqi(raw_comps, zone_type=zone_type)
        current_aqi = aqi_data.get("aqi", 0)

        trend_1h = None
        trend_24h = None
        
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
            
            if val_24h is not None:
                trend_24h = current_aqi - val_24h

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