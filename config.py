import os
import json
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()

_here = os.path.dirname(__file__)

def _load_json(fname: str) -> Dict[str, Any]:
    p = os.path.join(_here, fname)
    with open(p, "r") as f:
        return json.load(f)

ZONES = _load_json("zones.json")
AQI_BREAKPOINTS = _load_json("aqi_breakpoints.json")
airgradient_token = os.getenv("AIRGRADIENT_TOKEN")
jammu_airgradient_token = os.getenv("JAMMU_AIRGRADIENT_TOKEN")

SRINAGAR_AIRGRADIENT_CONFIG = {
    "location_id": 172681
}

JAMMU_AIRGRADIENT_CONFIG = {
    "location_id": 182171
}