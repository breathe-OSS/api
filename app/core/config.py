# SPDX-License-Identifier: MIT
#
# Copyright (C) 2026 The Breathe Open Source Project
# Copyright (C) 2026 sidharthify <wednisegit@gmail.com>
# Copyright (C) 2026 FlashWreck <theghost3370@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# loads the static json under app/data once at import time. these files are small
# and change only with a deploy, so there is no reason to read them per request.

import os
import json
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()

_data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _load_json(fname: str) -> Dict[str, Any]:
    """Read one json file out of app/data and return it as a dict."""
    path = os.path.join(_data_dir, fname)
    with open(path, "r") as handle:
        return json.load(handle)


ZONES = _load_json("zones.json")
AQI_BREAKPOINTS = _load_json("aqi_breakpoints.json")
NODES_CONFIG = _load_json("nodes.json")
SENSOR_INFO = _load_json("sensor_info.json")
FESTIVALS = _load_json("festivals.json")


def _sensor_coordinates(location_id: int, zone: Dict[str, Any]) -> Dict[str, float]:
    """Where a sensor actually sits, falling back to its zone's centre."""
    for sensor in SENSOR_INFO.get("sensors", []):
        if sensor.get("location_id") != location_id:
            continue

        if sensor.get("latitude") is None or sensor.get("longitude") is None:
            break

        return {"lat": sensor["latitude"], "lon": sensor["longitude"]}

    return {"lat": zone["lat"], "lon": zone["lon"]}


def _build_sensor_zones() -> Dict[str, Dict[str, Any]]:
    """Every enabled sensor as a zone of its own, keyed by its slug."""
    sensors = {}

    for zone_id, entry in NODES_CONFIG.items():
        zone = ZONES.get(zone_id)
        if not zone:
            continue

        nodes = [node for node in entry.get("nodes", []) if node.get("enabled", True)]

        for node in nodes:
            if len(nodes) > 1:
                history_key = f"{zone_id}_{node['name']}"
            else:
                history_key = zone_id

            sensors[node["id"]] = {
                "id": node["id"],
                "name": node["name"],
                "kind": "sensor",
                "provider": "airgradient",
                **_sensor_coordinates(node["location_id"], zone),
                "zone_type": zone.get("zone_type", "hills"),
                "district": node.get("district", zone.get("district")),
                "division": node.get("division", zone.get("division")),
                "location_id": node["location_id"],
                "legacy_zone": zone_id,
                "node_name": node["name"],
                "history_key": history_key,
            }

    return sensors


SENSOR_ZONES = _build_sensor_zones()

ESTIMATE_ZONES = {
    zone_id: {**zone, "kind": "estimate"}
    for zone_id, zone in ZONES.items()
    if zone.get("provider") != "airgradient"
}

PLACES = {**SENSOR_ZONES, **ESTIMATE_ZONES}
