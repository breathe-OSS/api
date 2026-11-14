# Breathe backend
A modular FastAPI backend designed to retrieve and standardize air quality data across the J&K & Ladakh region for the **BreatheOSS** clients. The system aggregates data from multiple providers: **OpenMeteo** for satellite-based estimates and **AirGradient** for high-precision ground sensors. It currently calculates AQI based on both **Indian (CPCB)** and **US EPA** standards.

The full API documentation (every endpoint, field and quirk, with examples) lives at [docs.breatheoss.app](https://docs.breatheoss.app). An interactive reference generated from the code is at [api.breatheoss.app/docs](https://api.breatheoss.app/docs).

## How the AQI is Calculated

`[1]` The system accepts a dictionary of raw pollutant values. Before any math occurs, the system sanitizes the input keys using a robust mapping strategy.

 - It handles variations in naming conventions (e.g., mapping "pm2.5", "pm25", or "pm2_5" all to the internal standard pm2_5).

 - This ensures that no data is dropped due to typo-sensitivity or API inconsistencies.

`[2]` The system calculates AQI using two major standards: **Indian (CPCB)** and **US EPA**. Each has specific unit requirements:

- **Indian AQI**: PM2.5, PM10, NO2, SO2 are maintained in µg/m³. Carbon Monoxide (CO) and Methane (CH4) are divided by 1000 to convert from µg/m³ to mg/m³.
- **US EPA AQI**: Gases (NO2, SO2, CO) are converted from µg/m³ to parts-per-billion (ppb) or parts-per-million (ppm) based on molar volume at 25°C.

- Methane (CH4) is tracked but currently not included in AQI calculations for either standard.

`[3]` Once units are standardized, the system calculates an individual Sub-Index for each pollutant using **Linear Interpolation**.

 - The system scans the `AQI_BREAKPOINTS` (Indian) or `US_BREAKPOINTS` configuration to find the specific range [Clo​,Chi​] that the current concentration falls into.
 - The system applies the standard AQI formula:

    $$I = \left[ \frac{I_{hi} - I_{lo}}{C_{hi} - C_{lo}} \times (C - C_{lo}) \right] + I_{lo}$$

  Where:
  - **I**: The calculated AQI sub-index.
  - **C**: The current pollutant concentration.
  - **Clo​/Chi​**: The concentration breakpoints (lower and upper bounds).
  - **Ilo​/Ihi**​: The corresponding AQI index breakpoints.

  The code includes failsafes: if a value exceeds the maximum defined breakpoint, it is capped at 500; if it is below the minimum, it defaults to 0.

`[4]` The final Air Quality Index is **not** an average of the pollutants.
  - The system identifies the maximum value among all calculated sub-indices.
  - The pollutant responsible for this highest value is flagged as the `main_pollutant`.
  - Both the Indian `aqi` and the `us_aqi` are calculated and returned in the final payload.

`[5]` **Averaging Windows**: Because official regulations (like the Indian CPCB and US EPA) define AQI using rolling averages rather than instantaneous spikes, the API processes both:
   - **Live/Instantaneous AQI**: Computed directly from current sensor values for real-time spike tracking.
   - **24-Hour Average AQI**: Computed by first aggregating historical raw concentrations over the past 24 hours, and then running those smoothed values back through the AQI formula.

## Structure
```
api/
├── main.py                     # Entry point
├── Procfile
├── requirements.txt
├── .env
├── breathe.db                  # Local SQLite database (auto-generated)
└── app/
    ├── __init__.py
    ├── api/                    # Routes & endpoints
    │   ├── __init__.py
    │   └── routes.py
    ├── core/                   # Config, database, cache, conversions
    │   ├── __init__.py
    │   ├── calibration.py
    │   ├── config.py
    │   ├── database.py
    │   ├── conversions.py
    │   └── redis_client.py
    ├── data/                   # Static JSON data files
    │   ├── __init__.py
    │   ├── zones.json
    │   ├── nodes.json          # AirGradient node configurations
    │   ├── sensor_info.json    # Hardware sensor metadata
    │   ├── aqi_breakpoints.json
    │   └── festivals.json      # Festival dates for spike protection
    └── services/               # Data fetching & processing
        ├── __init__.py
        ├── fetchers.py
        ├── seasonal.py
        └── weather.py
```

## Main modules
- `main.py`
  Initializes the FastAPI application and starts a background scheduler. This scheduler runs every 15 minutes to fetch fresh data for all zones, ensuring the app serves cached data instantly without hitting API rate limits during user requests. It also opens the Redis connection pool, and enables Sentry error reporting when `SENTRY_DSN` is set.
- `app/api/routes.py`
  Generates all `/aqi/<zone_id>` endpoints dynamically based on `zones.json`.
- `app/core/database.py`
  Handles data persistence using **PostgreSQL** (production) or **SQLite** (local development). Stores sensor readings, temperature, and humidity for historical graph plotting. Also handles Continuous Aggregations (15-minute rollups) via `refresh_15m_rollups` to serve historical queries with high performance. Also stores per-node offsets (`node_offsets`), recomputed daily. `/historical-data` uses them to correct multi-sensor zones, so the series doesn't jump when a sensor joins or drops out.
- `app/core/calibration.py`
  Puts every AirGradient sensor on the same PM2.5 correction, the US EPA 2021 formula with its 2022 extension for smoke. Each sensor can be set to a different correction on AirGradient's side, so it is decided per sensor and per day (IST). The calm readings, under 200 µg/m³ raw, are checked against the EPA formula worked out from their own raw PM2.5 and raw humidity. If at least 70% match, AirGradient was applying EPA and its values are kept for the whole day, spikes included. If 30% or fewer match, the EPA value is served for the whole day. In between, or with fewer than 6 calm readings, each reading is checked on its own. Live readings use the sensor's last 24 hours, stored readings their own day, and AirGradient's original value is kept in `pm2_5_ag`.
- `app/core/config.py`
  Loads environment variables and configuration from `zones.json`, `nodes.json`, `sensor_info.json`, `aqi_breakpoints.json`, and `festivals.json`.
- `app/core/conversions.py`
  Handles the mathematics of AQI calculation, unit conversions (e.g., CO from µg/m³ to mg/m³), and Indian CPCB sub-index mapping along with US EPA sub-index mapping.
- `app/core/redis_client.py`
  Manages the Redis connection pool used to cache the `/seasonal`, `/weather-history`, and `/historical-data` responses, and provides the Redis health check.
- `app/services/fetchers.py`
   Contains sophisticated data fetching logic:
   - `fetch_openmeteo_live`: Queries satellite-based pollutant data.
   - `fetch_multi_node_airgradient`: Averages data from multiple AirGradient nodes (e.g., Jammu and Srinagar), with built-in spike detection, grace periods for erratic nodes, and stale data protection.
   - `get_zone_data`: Implements a multi-layered caching strategy. It prioritize ground sensors but automatically falls back to satellite data if physical sensors are offline or reporting unrealistic spikes.
- `app/services/seasonal.py`
  Builds per-zone monthly climatology (average PM2.5, PM10, rainfall, and temperature per calendar month) by backfilling ~4 years of Open-Meteo reanalysis data. Refreshed automatically every 30 days by the background scheduler. Powers the `/seasonal/<zone_id>` endpoint so clients can show how pollution typically behaves across winter, monsoon, and summer.
- `app/services/weather.py`
  Fetches the live weather condition per zone and classifies it (`thunderstorm`, `rain`, `snow`, `fog`, `smog`, `clear`, `cloudy`). Fog with high PM2.5 is reported as smog. Generates a season-aware informational text included in the `/aqi/<zone_id>` payload under `weather`. Also serves condition-tagged historical buckets for the `/weather-history` endpoint so clients can filter AQI history by weather.
- `app/data/zones.json`
  Contains zone definitions including names, coordinates, and `zone_type` (`hills`, `urban`, `industrial`). `zone_type` is returned to clients as `zone_applied`, but it doesn't currently change the AQI calculation.
- `app/data/nodes.json`
  Maps specific zones to their physical sensor configurations (`location_id`) and dynamic API token environment variables (`token_env_var`).
- `app/data/sensor_info.json`
  Stores hardware-specific metadata for ground sensors (model, coordinates, installation dates).
- `app/data/aqi_breakpoints.json`
  Official Indian AQI breakpoint tables.
- `app/data/festivals.json`
  Diwali dates for each year. On these days the 1-hour spike checks are skipped, and the PM ceilings are raised to the sensor's hardware limit, so that real firecracker spikes aren't filtered out.

## Requirements
- python ≥ 3.10
- fastapi
- httpx
- python-dotenv
- uvicorn
- psycopg2-binary (for Postgres support)
- redis
- sentry-sdk
- A running Redis server

## Environment Variables
```
AIRGRADIENT_TOKEN=yourkeyhere
JAMMU_AIRGRADIENT_TOKEN=yourkeyhere
DATABASE_URL=postgres://... (optional, defaults to local sqlite)
UPSTASH_REDIS_URL=redis://... (optional, defaults to redis://localhost:6379)
SENTRY_DSN=... (optional, enables Sentry error reporting)
```

## Running
From the `api` directory:
`uvicorn main:app --reload`

Redis must be reachable. Without it, `/seasonal`, `/weather-history`, and `/historical-data` return errors.

## Endpoints
The short list is below. For response fields, time range syntax, errors and examples, see [docs.breatheoss.app](https://docs.breatheoss.app).

- **Zone Data**: Access data for a specific zone using its dynamic ID (e.g., `srinagar`, `jammu_city`, `leh`):
  `GET /aqi/<zone_id>`

- **Generic Lookup**:
  `GET /aqi/zone/{zone_id}`

- **Many Zones at Once**:
  `GET /aqi/zones?ids=srinagar,leh`
  The same payload as the generic lookup for every zone in `ids`, or for all zones when `ids` is left out, as `{"zones": [...]}`. Zones that fail are left out of the list.

- **List All Zones**:
  `GET /zones`

- **Hardware Sensor Metadata**:
  `GET /sensor-info`

- **Health Check**:
  `GET /health`
  Reports whether the database and Redis are reachable. Returns `503` with `"status": "degraded"` if either is down.

- **Seasonal Climatology**:
  `GET /seasonal/{zone_id}`
  Returns 12 monthly entries with typical PM2.5, PM10, AQI, rainfall (mm), and temperature for the zone, plus the current month and season label. Backed by Open-Meteo reanalysis (2022 to present). If a month has at least 200 ground sensor readings, its entry also includes the sensor averages (`sensor_pm2_5`, `sensor_pm10`). These sit alongside the reanalysis values rather than being blended into them.

- **Weather History**:
  `GET /weather-history/{zone_id}/{time_range}/{interval}`
  Returns time buckets tagged with a weather condition (`rain`, `snow`, `fog`, `thunderstorm`, `clear`, `cloudy`) and precipitation totals, aligned with `/historical-data` bucketing so clients can filter AQI history by weather. Example: `/weather-history/srinagar/1w/1h`. Also takes the calendar windows described below. Buckets start at `anchor + n * interval`, and `anchor` is `0` for rolling ranges. Takes `?limit=` and `?cursor=` the same way as `/historical-data`.

- **Historical Data**:
  `GET /historical-data/{location}/{time_range}/{interval}/{metrics}?format=json|csv&limit=&cursor=`
  Streams historical downsampled sensor readings. Supports memory-safe CSV or JSON streaming, fast-lane rollups (for 15m intervals), and standard Cache-Control headers to protect the DB from heavy loads. Example: `/historical-data/jammu_city/30d/15m/pm2.5,pm10?format=json`. Pass `limit` (1 to 10,000) to get one page of buckets instead of the whole stream, with `next_cursor` in the JSON (or an `X-Next-Cursor` header for CSV) to pass back as `cursor` for the next page. It's `null` on the last page.

- **Calendar Windows**:
  `time_range` on `/historical-data` and `/weather-history` also takes a fixed window instead of a rolling duration. Dates are read in IST, buckets are counted from the start of the window, and a malformed or backwards window returns `400`.
  - Month: `2026-10`
  - ISO week (Monday to Sunday): `2026-W42`
  - Day: `2026-10-15`
  - Span of days, both ends included: `2026-10-01_2026-10-07`

  Example: `/historical-data/jammu_city/2026-W42/1h/pm2.5,pm10?format=json`

## Development
The project is designed to be data-driven. Adding or modifying zones (e.g., converting a zone from satellite to physical AirGradient sensors) does not require changing Python code:

1. **Update `zones.json`**: Change the zone's `provider` (e.g., from `"openmeteo"` to `"airgradient"`).
2. **Update `nodes.json`**: Map the `zone_id` to its AirGradient API token environment variable (`token_env_var`) and list of physical sensor location IDs. A `token_env_var` containing `PUBLIC` (e.g. `PUBLIC_AIRGRADIENT_TOKEN`) reads the node from AirGradient's public feed, which needs no token.
3. **Update `sensor_info.json`**: Add the physical sensor metadata to expose it correctly on frontend maps.

Similarly, if government standards change, updating `aqi_breakpoints.json` will instantly update the calculation logic across the entire application.