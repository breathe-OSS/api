# Breathe backend
A modular fastAPI backend for retrieving air quality data across Jammu & Kashmir, used in the Breeze app, written in **Python**. Zone definitions and AQI breakpoints are stored in external JSON files, and endpoints are registered dynamically at startup.

## Structure
```breathe/
├─ api/
│  ├─ main.py
│  ├─ routes.py
│  ├─ fetchers.py
│  ├─ conversions.py
│  ├─ config.py
│  ├─ zones.json
│  ├─ aqi_breakpoints.json
│  └─ .env
```

## Main modules
- `main.py`
  Creates the fastapi application and registers routes.
- `routes.py`
  generates all `/aqi/<zone>` endpoints dynamically based on`zones.json`. also exposes `/aqi/zone/{zone_id}`.
- `fetchers.py`
   contains data fetch logic.
   `fetch_srinagar_gov` reads cpcb data from data.gov.in, plus temperature from openweather.
   `fetch_jammu_openweather` reads air pollution + weather temperature from openweather.
- `conversions.py`
  handles pollutant unit conversions, aqi calculation, breakpoints, and temperature corrections using the ideal gas law factor.
- `config.py`
  loads environment variables, zones.json, and aqi_breakpoints.json.
- `zones.json`
  contains all zone definitions with fixed ids, names, providers, and coordinates.
- `aqi_breakpoints.json`
  contains all us epa breakpoint tables for pm2.5, pm10, co, no2, so2, and o3.

## Requirements
- python ≥ 3.10
- fastapi
- httpx
- python-dotenv
- uvicorn

## Environment variables
set in `.env`:

```
DATA_GOV_API_KEY=your_key
OWM_API_KEY=your_key
```

## Running
From the `api` directory:
`uvicorn main:app --reload`

## Endpoints
Each zone in zones.json becomes an endpoint of the form:
```/aqi/<zone_id>```

`srinagar_gov` is also exposed as:
```/aqi/srinagar```

A generic lookup is also available:
```/aqi/zone/{zone_id}```

## How AQI calculation works

`[1].` Pollutant concentrations (µg/m³) are fetched from cpcb or openweather.

`[2].` Real time temperature is fetched from openweather and used to adjust concentrations for gases (co, no2, so2, o3) using a temperature correction factor derived from the ideal gas law; `(temp_k / 298.15)`.

`[3].` Corrected values are mapped to us AQI breakpoints.

`[4].` The highest pollutant AQI becomes the overall AQI.

## Development
Zone data and breakpoint data are fully external. Adding a new district or adjusting breakpoints requires no code changes, only JSON edits.
