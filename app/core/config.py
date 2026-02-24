import os
import json
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()

_data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

def _load_json(fname: str) -> Dict[str, Any]:
    p = os.path.join(_data_dir, fname)
    with open(p, "r") as f:
        return json.load(f)

ZONES = _load_json("zones.json")
AQI_BREAKPOINTS = _load_json("aqi_breakpoints.json")
airgradient_token = os.getenv("AIRGRADIENT_TOKEN")
jammu_airgradient_token = os.getenv("JAMMU_AIRGRADIENT_TOKEN") # this is currently being used for jammu and rajouri, and will probably will also be used for future sensors

SRINAGAR_AIRGRADIENT_NODES = [
    {"location_id": 172681, "name": "Kanipora"},
    {"location_id": 170398, "name": "Bemina"},
]

JAMMU_AIRGRADIENT_NODES = [
    {"location_id": 182171, "name": "Talab Tillo"},
    {"location_id": 184303, "name": "Gandhi Nagar"},
]

RAJOURI_AIRGRADIENT_CONFIG = {
    "location_id": 184149,
    "name": "Rajouri"
}
