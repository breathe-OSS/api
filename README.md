# Breathe backend
A modular FastAPI backend designed to retrieve and standardize air quality data across the J&K & Ladakh region for the **BreatheOSS** clients. The system aggregates data from multiple providers: **OpenMeteo** for satellite-based estimates and **AirGradient** for high-precision ground sensors. It currently calculates AQI based on both **Indian (CPCB)** and **US EPA** standards.

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
    ├── core/                   # Config, database, conversions
    │   ├── __init__.py
    │   ├── config.py
    │   ├── database.py
    │   └── conversions.py
    ├── data/                   # Static JSON data files
    │   ├── __init__.py
    │   ├── zones.json
    │   ├── nodes.json          # AirGradient node configurations
    │   ├── sensor_info.json    # Hardware sensor metadata
    │   └── aqi_breakpoints.json
    └── services/               # Data fetching & processing
        ├── __init__.py
        └── fetchers.py
```

## Main modules
- `main.py`
  Initializes the FastAPI application and starts a background scheduler. This scheduler runs every 15 minutes to fetch fresh data for all zones, ensuring the app serves cached data instantly without hitting API rate limits during user requests.
- `app/api/routes.py`
  Generates all `/aqi/<zone_id>` endpoints dynamically based on `zones.json`.
- `app/core/database.py`
  Handles data persistence using **PostgreSQL** (production) or **SQLite** (local development). Stores sensor readings, temperature, and humidity for historical graph plotting. Also handles Continuous Aggregations (15-minute rollups) via `refresh_15m_rollups` to serve historical queries with high performance.
- `app/core/config.py`
  Loads environment variables and configuration from `zones.json`, `nodes.json`, and `aqi_breakpoints.json`.
- `app/core/conversions.py`
  Handles the mathematics of AQI calculation, unit conversions (e.g., CO from µg/m³ to mg/m³), and Indian CPCB sub-index mapping along with US EPA sub-index mapping.
- `app/services/fetchers.py`
   Contains sophisticated data fetching logic:
   - `fetch_openmeteo_live`: Queries satellite-based pollutant data.
   - `fetch_multi_node_airgradient`: Averages data from multiple AirGradient nodes (e.g., Jammu and Srinagar), with built-in spike detection, grace periods for erratic nodes, and stale data protection.
   - `get_zone_data`: Implements a multi-layered caching strategy. It prioritize ground sensors but automatically falls back to satellite data if physical sensors are offline or reporting unrealistic spikes.
- `app/data/zones.json`
  Contains zone definitions including names, coordinates, and `zone_type` (`hills`, `urban`, `industrial`) for customized AQI calculation.
- `app/data/nodes.json`
  Maps specific zones to their physical sensor configurations (`location_id`) and dynamic API token environment variables (`token_env_var`).
- `app/data/sensor_info.json`
  Stores hardware-specific metadata for ground sensors (model, coordinates, installation dates).
- `app/data/aqi_breakpoints.json`
  Official Indian AQI breakpoint tables.

## Requirements
- python ≥ 3.10
- fastapi
- httpx
- python-dotenv
- uvicorn
- psycopg2-binary (for Postgres support)

## Environment Variables
```
AIRGRADIENT_TOKEN=yourkeyhere
JAMMU_AIRGRADIENT_TOKEN=yourkeyhere
DATABASE_URL=postgres://... (optional, defaults to local sqlite)
```

## Running
From the `api` directory:
`uvicorn main:app --reload`

## Endpoints
- **Zone Data**: Access data for a specific zone using its dynamic ID (e.g., `srinagar`, `jammu_city`, `leh`):
  `GET /aqi/<zone_id>`

- **Generic Lookup**:
  `GET /aqi/zone/{zone_id}`

- **List All Zones**:
  `GET /zones`

- **Hardware Sensor Metadata**:
  `GET /sensor-info`

- **Historical Data**:
  `GET /historical-data/{location}/{time_range}/{interval}/{metrics}?format=json|csv`
  Streams historical downsampled sensor readings. Supports memory-safe CSV or JSON streaming, fast-lane rollups (for 15m intervals), and standard Cache-Control headers to protect the DB from heavy loads. Example: `/historical-data/jammu-city/30d/15m/pm2.5,pm10?format=json`

## Development
The project is designed to be data-driven. Adding or modifying zones (e.g., converting a zone from satellite to physical AirGradient sensors) does not require changing Python code:

1. **Update `zones.json`**: Change the zone's `provider` (e.g., from `"openmeteo"` to `"airgradient"`).
2. **Update `nodes.json`**: Map the `zone_id` to its AirGradient API token environment variable (`token_env_var`) and list of physical sensor location IDs.
3. **Update `sensor_info.json`**: Add the physical sensor metadata to expose it correctly on frontend maps.

Similarly, if government standards change, updating `aqi_breakpoints.json` will instantly update the calculation logic across the entire application.