import httpx
import asyncio
from typing import List, Tuple, Dict, Any

async def fetch_openmeteo_batch(client: httpx.AsyncClient, lats: List[float], lons: List[float]) -> List[dict]:
    weather_url = "https://api.open-meteo.com/v1/forecast"
    weather_params = {
        "latitude": ",".join(map(str, lats)),
        "longitude": ",".join(map(str, lons)),
        "current": "temperature_2m,wind_speed_10m,wind_direction_10m",
        "timezone": "auto"
    }

    try:
        response = await client.get(weather_url, params=weather_params)
        if response.status_code == 200:
            weather_res = response.json()
            if isinstance(weather_res, list):
                return weather_res
            else:
                return [weather_res]
    except Exception as e:
        print(f"Weather batch request failed: {e}")
        
    return []

async def fetch_grid_data(points: List[Tuple[float, float]]) -> List[Dict[str, Any]]:
    CHUNK_SIZE = 100
    all_grid_data = []
    
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(points), CHUNK_SIZE):
            chunk = points[i:i + CHUNK_SIZE]
            lats = [p[0] for p in chunk]
            lons = [p[1] for p in chunk]
            
            try:
                weather_list = await fetch_openmeteo_batch(client, lats, lons)
                
                for point_idx in range(len(chunk)):
                    lat, lon = chunk[point_idx]
                    
                    point_data = {
                        "lat": lat,
                        "lon": lon,
                        "t": None,
                        "ws": None,
                        "wd": None
                    }
                    
                    if weather_list and point_idx < len(weather_list):
                        w_curr = weather_list[point_idx].get("current", {})
                        if w_curr:
                            point_data["t"] = w_curr.get("temperature_2m")
                            point_data["ws"] = w_curr.get("wind_speed_10m")
                            point_data["wd"] = w_curr.get("wind_direction_10m")
                            
                    if point_data["t"] is not None:
                        all_grid_data.append(point_data)
                        
            except Exception as e:
                print(f"Error fetching grid chunk {i}: {e}")
            
            # small delay to prevent aggressive rate limits
            await asyncio.sleep(0.5)
            
    return all_grid_data
