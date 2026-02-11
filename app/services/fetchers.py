import httpx
import asyncio
from datetime import datetime, timedelta
from fastapi import HTTPException
from typing import Dict, Any, List

from app.core.config import ZONES, SRINAGAR_AIRGRADIENT_NODES, JAMMU_AIRGRADIENT_NODES, RAJOURI_AIRGRADIENT_CONFIG, airgradient_token, jammu_airgradient_token
from app.core.conversions import calculate_overall_aqi
from app.core import database

_RAM_CACHE = {}
_SPIKE_CACHE = {}
CACHE_DURATION = 900  # 15 minutes
SPIKE_GRACE_PERIOD = 3600  # 1 hour

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

async def fetch_multi_node_airgradient(
    zone_id: str,
    nodes: List[Dict],
    token: str,
    lat: float,
    lon: float,
    zone_type: str = "urban"
) -> Dict[str, Any]:
    if not token:
        raise HTTPException(status_code=500, detail=f"Missing AG Token for {zone_id}")

    current_time = datetime.now().timestamp()
    MAX_AGE_SECONDS = 3600  # 1 hour
    
    async with httpx.AsyncClient(timeout=20) as client:
        curr_tasks = []
        for node in nodes:
            url = f"https://api.airgradient.com/public/api/v1/locations/{node['location_id']}/measures/current?token={token}"
            curr_tasks.append(client.get(url))
        
        om_url = "https://air-quality-api.open-meteo.com/v1/air-quality"
        om_params = {
            "latitude": lat, "longitude": lon,
            "hourly": "ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide",
            "timezone": "auto", "timeformat": "unixtime", "past_days": 1
        }
        curr_tasks.append(client.get(om_url, params=om_params))
        
        results = await asyncio.gather(*curr_tasks, return_exceptions=True)
    
    om_resp = results[-1]
    sensor_responses = results[:-1]
    
    valid_readings = []
    node_statuses = []
    spike_warnings = []
    
    for i, resp in enumerate(sensor_responses):
        node_cfg = nodes[i]
        node_name = node_cfg.get("name", f"Node{i+1}")
        node_cache_key = f"{zone_id}_{node_name}"
        
        if node_cache_key in _SPIKE_CACHE:
            spike_time = _SPIKE_CACHE[node_cache_key]
            time_since_spike = current_time - spike_time
            
            if time_since_spike < SPIKE_GRACE_PERIOD:
                remaining_minutes = int((SPIKE_GRACE_PERIOD - time_since_spike) / 60)
                node_statuses.append({"node": node_name, "status": "grace_period", "remaining_minutes": remaining_minutes})
                spike_warnings.append(f"{node_name}: excluded due to recent spike (grace period: {remaining_minutes} mins remaining)")
                continue
            else:
                del _SPIKE_CACHE[node_cache_key]
        
        if isinstance(resp, Exception):
            node_statuses.append({"node": node_name, "status": "error"})
            continue
            
        if resp.status_code != 200:
            node_statuses.append({"node": node_name, "status": "offline"})
            continue
        
        data = resp.json()
        pm25 = data.get("pm02_corrected") or data.get("pm02")
        pm10 = data.get("pm10_corrected") or data.get("pm10")
        
        if pm25 is None:
            node_statuses.append({"node": node_name, "status": "no_data"})
            continue
        
        # Check data freshness
        ag_timestamp = data.get("timestamp")
        data_age = None
        reading_ts = current_time
        
        if ag_timestamp:
            try:
                dt = datetime.fromisoformat(ag_timestamp.replace("Z", "+00:00"))
                reading_ts = dt.timestamp()
                data_age = current_time - reading_ts
            except:
                pass
        
        if data_age and data_age > MAX_AGE_SECONDS:
            node_statuses.append({"node": node_name, "status": "stale", "age_minutes": int(data_age/60)})
            continue
        
        pm25_val = float(pm25)
        pm10_val = float(pm10) if pm10 else 0.0
        
        if pm25_val > 650 or pm10_val > 600:
            node_statuses.append({"node": node_name, "status": "spike_detected", "pm2_5": pm25_val, "pm10": pm10_val})
            spike_warnings.append(f"{node_name}: absolute threshold exceeded (PM2.5={pm25_val:.0f} or PM10={pm10_val:.0f})")
            _SPIKE_CACHE[node_cache_key] = current_time
            continue
        
        node_zone_id = f"{zone_id}_{node_name}"
        node_history = database.get_history(node_zone_id, hours=2)
        
        if node_history:
            target_ts = reading_ts - 3600
            closest_reading = min(node_history, key=lambda h: abs(h["ts"] - target_ts))
            
            time_diff = abs(closest_reading["ts"] - target_ts)
            if time_diff < 5400:
                pm25_1h_ago = closest_reading["pm2_5"]
                pm25_jump = pm25_val - pm25_1h_ago
                
                if pm25_jump > 200:
                    node_statuses.append({"node": node_name, "status": "spike_detected", "pm25_jump": int(pm25_jump)})
                    spike_warnings.append(f"{node_name}: sudden spike detected (PM2.5 jumped +{int(pm25_jump)} in 1 hour)")
                    _SPIKE_CACHE[node_cache_key] = current_time
                    continue
        
        valid_readings.append({
            "pm2_5": pm25_val,
            "pm10": pm10_val,
            "temp": data.get("atmp_corrected") or data.get("atmp"),
            "humidity": data.get("rhum_corrected") or data.get("rhum"),
            "timestamp": reading_ts,
            "node_name": node_name
        })
        node_statuses.append({"node": node_name, "status": "active"})
        
        database.save_reading(f"{zone_id}_{node_name}", pm25_val, pm10_val, timestamp=reading_ts)
    
    if not valid_readings:
        raise ValueError(f"All {len(nodes)} sensor nodes offline or showing spikes")
    
    # Average the valid readings
    merged_pm25 = sum(r["pm2_5"] for r in valid_readings) / len(valid_readings)
    merged_pm10 = sum(r["pm10"] for r in valid_readings) / len(valid_readings)
    
    temp_readings = [r["temp"] for r in valid_readings if r["temp"] is not None]
    humidity_readings = [r["humidity"] for r in valid_readings if r["humidity"] is not None]
    
    current_comps = {
        "pm2_5": merged_pm25,
        "pm10": merged_pm10,
        "temp": sum(temp_readings) / len(temp_readings) if temp_readings else None,
        "humidity": sum(humidity_readings) / len(humidity_readings) if humidity_readings else None,
        "_ag_timestamp": max(r["timestamp"] for r in valid_readings),
        "_node_count": len(valid_readings),
        "_total_nodes": len(nodes),
        "_node_statuses": node_statuses
    }
    
    if spike_warnings:
        current_comps["_spike_warning"] = f"Data from {len(valid_readings)} of {len(nodes)} sensors. " + "; ".join(spike_warnings)
    
    database.save_reading(zone_id, merged_pm25, merged_pm10)
    
    om_points = []
    if isinstance(om_resp, Exception) or om_resp.status_code != 200:
        print(f"OM request failed for {zone_id}")
    else:
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
            if zone_id == "srinagar":
                nodes = SRINAGAR_AIRGRADIENT_NODES
                token = airgradient_token
            elif zone_id == "jammu_city":
                nodes = JAMMU_AIRGRADIENT_NODES
                token = jammu_airgradient_token
            else:  
                nodes = [RAJOURI_AIRGRADIENT_CONFIG]  
                token = jammu_airgradient_token
            
            try:
                if len(nodes) > 1:
                    fetched_data = await fetch_multi_node_airgradient(
                        zone_id=zone_id,
                        nodes=nodes,
                        token=token,
                        lat=lat,
                        lon=lon,
                        zone_type=zone_type
                    )
                    source_name = f"airgradient ({fetched_data['current_comps']['_node_count']}/{fetched_data['current_comps']['_total_nodes']} sensors) + openmeteo"
                    
                    if "_spike_warning" in fetched_data["current_comps"]:
                        sensor_offline_warning = fetched_data["current_comps"]["_spike_warning"]
                else:
                    config = nodes[0]
                    fetched_data = await fetch_airgradient_common(
                        zone_id=zone_id,
                        loc_id=config["location_id"],
                        token=token,
                        lat=lat,
                        lon=lon,
                        zone_type=zone_type
                    )
                    source_name = "airgradient + openmeteo"
                
                pm25 = fetched_data["current_comps"].get("pm2_5")
                if pm25 is None:
                    raise ValueError("No PM2.5 data from sensor")
                
                # Older than 1 hour = sensor offline (for single node or all nodes)
                ag_timestamp = fetched_data["current_comps"].get("_ag_timestamp")
                if ag_timestamp:
                    data_age = current_time - ag_timestamp
                    if data_age > 3600:  # 1 hour
                        raise ValueError(f"Sensor data is stale ({int(data_age/60)} minutes old)")
                    
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
