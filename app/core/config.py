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
