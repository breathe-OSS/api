import asyncio
from datetime import datetime
import sentry_sdk

GLOBAL_GRID_CACHE = {
    "updated_at": None,
    "grid": []
}

LAT_MIN, LAT_MAX = 32.2, 37.1
LON_MIN, LON_MAX = 72.5, 80.4

# 30km resolution 
LAT_STEP = 0.27
LON_STEP = 0.324

def generate_grid_points():
    points = []
    lat = LAT_MIN
    while lat <= LAT_MAX:
        lon = LON_MIN
        while lon <= LON_MAX:
            points.append((round(lat, 3), round(lon, 3)))
            lon += LON_STEP
        lat += LAT_STEP
    return points

GRID_POINTS = generate_grid_points()

async def update_grid_cache():
    from app.services.grid_fetcher import fetch_grid_data
    try:
        new_data = await fetch_grid_data(GRID_POINTS)
        if new_data:
            GLOBAL_GRID_CACHE["grid"] = new_data
            GLOBAL_GRID_CACHE["updated_at"] = int(datetime.now().timestamp())
            print(f"Grid cache updated with {len(new_data)} points.")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        print(f"Failed to update grid cache: {e}")

async def periodic_grid_updates():
    await update_grid_cache()
    while True:
        # Wait 1 hour (3600 seconds)
        await asyncio.sleep(3600)
        try:
            await update_grid_cache()
        except asyncio.CancelledError:
            break
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print(f"CRITICAL: Grid background loop error: {e}")
