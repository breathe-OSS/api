import os
from typing import Dict, Any, Optional, Tuple

import httpx
import json
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="breathe backend")

data_gov_api_key = os.getenv("DATA_GOV_API_KEY")
owm_api_key = os.getenv("OWM_API_KEY")

if not data_gov_api_key:
    print("warning: DATA_GOV_API_KEY not set")
if not owm_api_key:
    print("warning: OWM_API_KEY not set")

###### ZONE DEFINITIONS ######

zones_path = os.path.join(os.path.dirname(__file__), "zones.json")
with open(zones_path, "r") as f:
    ZONES = json.load(f)
}

###### US AQI CALCULATION LOGIC ######

# breakpoints
bp_path = os.path.join(os.path.dirname(__file__), "aqi_breakpoints.json")
with open(bp_path, "r") as f:
    AQI_BREAKPOINTS = json.load(f)

def linear_interpolate(c: float, bp: Tuple[float, float, int, int]) -> int:
    c_lo, c_hi, i_lo, i_hi = bp
    if c_hi - c_lo == 0:
        return i_lo
    val = ((i_hi - i_lo) / (c_hi - c_lo)) * (c - c_lo) + i_lo
    return int(round(val))

def get_single_pollutant_aqi(pollutant: str, conc: float) -> Optional[int]:
    if pollutant not in AQI_BREAKPOINTS:
        return None
    bps = AQI_BREAKPOINTS[pollutant]
    if conc < bps[0][0]:
        return 0
    for bp in bps:
        if bp[0] <= conc <= bp[1]:
            return linear_interpolate(conc, bp)
    last_bp = bps[-1]
    if conc > last_bp[1]:
        return last_bp[3]
    return None

def convert_to_us_units(pollutant: str, val_ugm3: float, temp_k: float = 298.15) -> float:
    if pollutant == "co":
        base = val_ugm3 / 1145.0
        return base * (temp_k / 298.15)
    elif pollutant == "no2":
        base = val_ugm3 / 1.88
        return base * (temp_k / 298.15)
    elif pollutant == "so2":
        base = val_ugm3 / 2.62
        return base * (temp_k / 298.15)
    elif pollutant == "o3":
        base = val_ugm3 / 1.96
        return base * (temp_k / 298.15)
    return val_ugm3

def calculate_overall_aqi(pollutants_ugm3: Dict[str, float], temp_k: float = 298.15) -> Dict[str, Any]:
    aqi_details = {}
    concentrations_formatted = {}

    key_map = {
        "pm2.5": "pm2_5", "pm2_5": "pm2_5", "pm25": "pm2_5",
        "pm10": "pm10",
        "co": "co",
        "no2": "no2",
        "so2": "so2",
        "o3": "o3", "ozone": "o3"
    }

    for raw_key, val in pollutants_ugm3.items():
        k = raw_key.lower()
        if k in key_map:
            internal_key = key_map[k]
            converted_val = convert_to_us_units(internal_key, val, temp_k)
            concentrations_formatted[internal_key] = round(converted_val, 2)
            aqi_val = get_single_pollutant_aqi(internal_key, converted_val)
            if aqi_val is not None:
                aqi_details[internal_key] = aqi_val

    overall_aqi = 0
    main_pollutant = "n/a"

    if aqi_details:
        main_pollutant = max(aqi_details, key=aqi_details.get)
        overall_aqi = aqi_details[main_pollutant]

    return {
        "us_aqi": overall_aqi,
        "main_pollutant": main_pollutant,
        "aqi_breakdown": aqi_details,
        "concentrations_us_units": concentrations_formatted,
        "concentrations_raw_ugm3": pollutants_ugm3
    }

###### ENDPOINTS ######

@app.get("/zones")
def list_zones() -> Dict[str, Any]:
    return {
        "zones": [
            {
                "id": z["id"],
                "name": z["name"],
                "provider": z["provider"],
                "lat": z["lat"],
                "lon": z["lon"],
            }
            for z in ZONES.values()
        ]
    }

async def fetch_srinagar_gov() -> Dict[str, Any]:
    if not data_gov_api_key:
        raise HTTPException(status_code=500, detail="DATA_GOV_API_KEY not configured")

    resource_id = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
    params = {
        "api-key": data_gov_api_key,
        "format": "json",
        "limit": 100,
        "filters[state]": "jammu_and_kashmir",
        "filters[city]": "srinagar",
        "filters[station]": "rajbagh, srinagar - jkspcb",
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

    temp_k = 298.15
    if owm_api_key:
        s_lat = ZONES["srinagar_gov"]["lat"]
        s_lon = ZONES["srinagar_gov"]["lon"]
        weather_url = "https://api.openweathermap.org/data/2.5/weather"
        wparams = {"lat": s_lat, "lon": s_lon, "appid": owm_api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                wr = await client.get(weather_url, params=wparams)
            if wr.status_code == 200:
                wdata = wr.json()
                temp_k = wdata.get("main", {}).get("temp", temp_k)
        except Exception:
            temp_k = 298.15

    aqi_data = calculate_overall_aqi(raw_pollutants, temp_k)

    return {
        "zone_id": "srinagar_gov",
        "zone_name": "Srinagar (Rajbagh - JKSPCB)",
        "source": "CPCB (data.gov.in)",
        "last_update": last_update,
        "coordinates": {"lat": lat, "lon": lon},
        **aqi_data
    }

async def fetch_jammu_openweather(zone_id: str, zone_name: str, lat: float, lon: float):
    if not owm_api_key:
        raise HTTPException(status_code=500, detail="OWM_API_KEY not configured")

    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    params = {"lat": lat, "lon": lon, "appid": owm_api_key}

    temp_k = 298.15
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)

        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="openweather request failed")

        data = r.json()
        lst = data.get("list", [])
        if not lst:
            raise HTTPException(status_code=404, detail="no openweather aq data")

        entry = lst[0]
        dt = entry.get("dt")
        raw_comps = entry.get("components", {})

        weather_url = "https://api.openweathermap.org/data/2.5/weather"
        wparams = {"lat": lat, "lon": lon, "appid": owm_api_key}
        try:
            wr = await client.get(weather_url, params=wparams)
            if wr.status_code == 200:
                wdata = wr.json()
                temp_k = wdata.get("main", {}).get("temp", temp_k)
        except Exception:
            temp_k = 298.15

    aqi_data = calculate_overall_aqi(raw_comps, temp_k)

    return {
        "zone_id": zone_id,
        "zone_name": zone_name,
        "source": "openweather air pollution api",
        "timestamp_unix": dt,
        "coordinates": {"lat": lat, "lon": lon},
        **aqi_data
    }

@app.get("/aqi/srinagar")
async def get_srinagar_aqi():
    return await fetch_srinagar_gov()

@app.get("/aqi/jammu-gandhinagar")
async def get_jammu_aqi():
    z = ZONES["jammu_gandhinagar"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/budgam_town")
async def get_budgam_town_aqi():
    z = ZONES["budgam_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/ganderbal_town")
async def get_ganderbal_town_aqi():
    z = ZONES["ganderbal_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/anantnag_city")
async def get_anantnag_city_aqi():
    z = ZONES["anantnag_city"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/pulwama_town")
async def get_pulwama_town_aqi():
    z = ZONES["pulwama_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/shopian_town")
async def get_shopian_town_aqi():
    z = ZONES["shopian_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/kulgam_town")
async def get_kulgam_town_aqi():
    z = ZONES["kulgam_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/baramulla_town")
async def get_baramulla_town_aqi():
    z = ZONES["baramulla_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/kupwara_town")
async def get_kupwara_town_aqi():
    z = ZONES["kupwara_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/bandipora_town")
async def get_bandipora_town_aqi():
    z = ZONES["bandipora_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/samba_town")
async def get_samba_town_aqi():
    z = ZONES["samba_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/kathua_town")
async def get_kathua_town_aqi():
    z = ZONES["kathua_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/udhampur_city")
async def get_udhampur_city_aqi():
    z = ZONES["udhampur_city"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/reasi_town")
async def get_reasi_town_aqi():
    z = ZONES["reasi_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/ramban_town")
async def get_ramban_town_aqi():
    z = ZONES["ramban_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/doda_town")
async def get_doda_town_aqi():
    z = ZONES["doda_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/kishtwar_town")
async def get_kishtwar_town_aqi():
    z = ZONES["kishtwar_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/rajouri_town")
async def get_rajouri_town_aqi():
    z = ZONES["rajouri_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/poonch_town")
async def get_poonch_town_aqi():
    z = ZONES["poonch_town"]
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])

@app.get("/aqi/zone/{zone_id}")
async def get_zone_aqi(zone_id: str):
    if zone_id not in ZONES:
        raise HTTPException(status_code=404, detail="zone not found")
    z = ZONES[zone_id]
    provider = z.get("provider", "")
    if provider == "cpcb_data_gov":
        return await fetch_srinagar_gov()
    return await fetch_jammu_openweather(z["id"], z["name"], z["lat"], z["lon"])