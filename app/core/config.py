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

_nodes_config = _load_json("nodes.json")

SRINAGAR_AIRGRADIENT_NODES = _nodes_config.get("SRINAGAR_AIRGRADIENT_NODES", [])
JAMMU_AIRGRADIENT_NODES = _nodes_config.get("JAMMU_AIRGRADIENT_NODES", [])
RAJOURI_AIRGRADIENT_CONFIG = _nodes_config.get("RAJOURI_AIRGRADIENT_CONFIG", {})
