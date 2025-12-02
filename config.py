import os
import json
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()

data_gov_api_key = os.getenv("DATA_GOV_API_KEY")
owm_api_key = os.getenv("OWM_API_KEY")

_here = os.path.dirname(__file__)

def _load_json(fname: str) -> Dict[str, Any]:
    p = os.path.join(_here, fname)
    with open(p, "r") as f:
        return json.load(f)

ZONES = _load_json("zones.json")
AQI_BREAKPOINTS = _load_json("aqi_breakpoints.json")