import os
import json
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()

data_gov_api_key = os.getenv("DATA_GOV_API_KEY")

_here = os.path.dirname(__file__)

def _load_json(fname: str) -> Dict[str, Any]:
    p = os.path.join(_here, fname)
    with open(p, "r") as f:
        return json.load(f)

ZONES = _load_json("zones.json")
AQI_BREAKPOINTS = _load_json("aqi_breakpoints.json")
openaq_api_key = os.getenv("OPENAQ_API_KEY")

# Configuration for Srinagar CPCB Station (OpenAQ)
SRINAGAR_OPENAQ_CONFIG = {
    "location_id": 220265,
    "sensor_map": {
        12251174: "pm2_5",
        12251173: "pm10",
        12251176: "so2",
        12251169: "co",
    }
}