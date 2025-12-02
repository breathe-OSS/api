import httpx
from fastapi import HTTPException
from typing import Dict, Any

from config import data_gov_api_key, owm_api_key, ZONES
from conversions import calculate_overall_aqi

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