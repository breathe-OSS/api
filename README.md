# Breathe backend
A modular FastAPI backend designed to retrieve and standardize air quality data across the Jammu & Kashmir region for the **Breathe** app. The system aggregates data from multiple providers: **OpenMeteo** for satellite-based estimates in most districts. And **Crowdsourced Ground Sensors** for ground air quality data in Jammu City and Srinagar.

## How the AQI is Calculated

`[1]` The system accepts a dictionary of raw pollutant values. Before any math occurs, the system sanitizes the input keys using a robust mapping strategy.

 - It handles variations in naming conventions (e.g., mapping "pm2.5", "pm25", or "pm2_5" all to the internal standard pm2_5).

 - This ensures that no data is dropped due to typo-sensitivity or API inconsistencies.

`[2]` Indian AQI standards require specific units for different chemical compounds. The system applies a check (`prepare_for_indian_aqi`) to the raw concentrations (C).

- PM2.5, PM10, NO2​, SO2​, and Ozone (O3​) are maintained in Micrograms per cubic meter (µg/m3).

- The code explicitly detects Carbon Monoxide (co) and divides the value by 1000. This converts the raw µg/m3 value into Milligrams per cubic meter (mg/m3), which is the required unit for the CO breakpoint table.

`[3]` Once units are standardized, the system calculates an individual Sub-Index for each pollutant. It does not simply "lookup" a value; it calculates a precise integer using **Linear Interpolation**.

 - The system scans the `AQI_BREAKPOINTS` configuration to find the specific range [Clo​,Chi​] that the current concentration falls into.
 - The system applies the standard AQI formula used by environmental agencies:

    `I=[(Chi​−Clo​)(Ihi​−Ilo​)​×(C−Clo​)]+Ilo​`

  Where:
  - **I**: The calculated AQI sub-index.
  - **C**: The current pollutant concentration.
  - **Clo​/Chi​**: The concentration breakpoints (lower and upper bounds).
  - **Ilo​/Ihi**​: The corresponding AQI index breakpoints.

  The code includes failsafes: if a value exceeds the maximum defined breakpoint, it is capped at 500; if it is below the minimum, it defaults to 0.

`[4]` The final Air Quality Index is **not** an average of the pollutants.
  - The system collects all calculated sub-indices (`aqi_details`). It then identifies the maximum value among them.
  - The pollutant responsible for this highest value is flagged as the `main_pollutant`.
  - This **single highest value** becomes the reported Overall AQI.

## Structure
```breathe/
├─ api/
│  ├─ main.py
│  ├─ routes.py
│  ├─ database.py
│  ├─ fetchers.py
│  ├─ conversions.py
│  ├─ config.py
│  ├─ zones.json
│  ├─ aqi_breakpoints.json
│  ├─ aqi_us_breakpoints.json
│  └─ .env
```

## Main modules
- `main.py`
  Initializes the FastAPI application and starts a background scheduler. This scheduler runs every 15 minutes to fetch fresh data for all zones, ensuring the app serves cached data instantly without hitting API rate limits during user requests.
- `routes.py`
  Generates all `/aqi/<zone>` endpoints dynamically based on`zones.json`. Also exposes `/aqi/zone/{zone_id}`.
- `database.py`
  Holds the code for our Postgres database storage for graph plotting and history.
- `fetchers.py`
   contains data fetch logic.
   `fetch_openmeteo_live` queries the OpenMeteo Air Quality API for a precise real-time satellite-based pollutant data.
   `fetch_airgradient_common` holds the common code required to call the AirGradient API for every zone.
   `fetch_airgradient_jammu` and `fetch_airgradient_srinagar` call the specific API tokens for  both regions.
   `get_zone_data` implements the caching strategy. it checks the internal server memory (RAM) first. If data is missing or older than 15 minutes, it fetches fresh data from the provider and updates the cache.
- `conversions.py`
  Handles the mathematics of AQI calculation.
  - Converts Carbon Monoxide (CO) from µg/m³ to mg/m³ to match Indian standards.
  - Maps raw concentrations to the official Indian CPCB sub-indices.
  - Determines the final AQI based on the dominant pollutant.
- `config.py`
  loads environment variables, zones.json, and aqi_breakpoints.json.
- `zones.json`
  contains all zone definitions with fixed ids, names, providers, and coordinates.
- `aqi_breakpoints.json`
  contains all indian aqi breakpoint tables for pm2.5, pm10, co, no2, so2, and o3.

## Requirements
- python ≥ 3.10
- fastapi
- httpx
- python-dotenv
- uvicorn

## Environment Variables
```
AIRGRADIENT_TOKEN=yourkeyhere
JAMMU_AIRGRADIENT_TOKEN=yourkeyhere
```

## Running
From the `api` directory:
`uvicorn main:app --reload`

## Endpoints
- Zone-Specific Data: Access data for a specific zone using its ID (defined in `zones.json`):
`GET /aqi/<zone_id>`

- Srinagar Special Endpoint:
`GET /aqi/srinagar`

- Jammu Special Endpoint:
`GET /aqi/jammu_city`

- Generic Lookup:
`GET /aqi/zone/{zone_id}`

- List All Zones:
`GET /zones`

## Development
The project is designed to be data-driven. Adding a new town or district does not require changing Python code; you simply add a new entry to `zones.json`. Similarly, if government standards change, updating `aqi_breakpoints.json` will instantly update the calculation logic across the entire application.