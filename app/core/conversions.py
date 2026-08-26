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

# turns raw concentrations into air quality index values.
#
# two scales are computed side by side for every reading. the indian CPCB index
# comes from app/data/aqi_breakpoints.json, the US EPA one from the table below.
# neither is derived from the other: they use different bands, different
# averaging assumptions and different units, so the same air can read 104 on one
# and 60 on the other. both are returned and the client picks.

import math
from typing import Any, Dict, Optional, Tuple

from app.core.config import AQI_BREAKPOINTS

# US EPA breakpoints, as (concentration low, concentration high, index low,
# index high). the pm2.5 rows are the 2024 revision, where Good ends at 9.0
# rather than the older 12.0.
#
# gases are in ppb here, except co which is in ppm. anything arriving in ug/m3
# has to go through _ugm3_to_ppb first.
US_BREAKPOINTS = {
    "pm2_5": [
        (0.0, 9.0, 0, 50),
        (9.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200),
        (125.5, 225.4, 201, 300),
        (225.5, 325.4, 301, 400),
        (325.5, 500.4, 401, 500),
    ],
    "pm10": [
        (0, 54, 0, 50),
        (55, 154, 51, 100),
        (155, 254, 101, 150),
        (255, 354, 151, 200),
        (355, 424, 201, 300),
        (425, 504, 301, 400),
        (505, 604, 401, 500),
    ],
    "no2": [
        (0, 53, 0, 50),
        (54, 100, 51, 100),
        (101, 360, 101, 150),
        (361, 649, 151, 200),
        (650, 1249, 201, 300),
        (1250, 1649, 301, 400),
        (1650, 2049, 401, 500),
    ],
    "so2": [
        (0, 35, 0, 50),
        (36, 75, 51, 100),
        (76, 185, 101, 150),
        (186, 304, 151, 200),
        (305, 604, 201, 300),
        (605, 804, 301, 400),
        (805, 1004, 401, 500),
    ],
    "co": [
        (0.0, 4.4, 0, 50),
        (4.5, 9.4, 51, 100),
        (9.5, 12.4, 101, 150),
        (12.5, 15.4, 151, 200),
        (15.5, 30.4, 201, 300),
        (30.5, 40.4, 301, 400),
        (40.5, 50.4, 401, 500),
    ],
}

# molar volume of an ideal gas at 25C and one atmosphere, in litres per mole.
_MOLAR_VOLUME = 24.45

# molar masses in grams per mole, used to get from mass to volume mixing ratio.
_MW = {
    "no2": 46.0055,
    "so2": 64.066,
    "co": 28.010,
}


def _ugm3_to_ppb(pollutant: str, ugm3: float) -> Optional[float]:
    """Convert a gas from ug/m3 to ppb, or to ppm for co. None if not a gas."""
    if pollutant not in _MW:
        return None

    ppb = ugm3 * _MOLAR_VOLUME / _MW[pollutant]

    # the epa co table is written in parts per million, everything else in
    # parts per billion.
    if pollutant == "co":
        return ppb / 1000.0

    return ppb


def linear_interpolate(c: float, bp: Tuple[float, float, int, int]) -> int:
    """Place a concentration along one breakpoint band and return its index."""
    c_lo, c_hi, i_lo, i_hi = bp

    # a zero width band would divide by zero. it should not happen with the
    # tables above, but a hand edited json could produce one.
    if c_hi == c_lo:
        return i_lo

    val = ((i_hi - i_lo) / (c_hi - c_lo)) * (c - c_lo) + i_lo
    return int(val)


def get_us_aqi(pollutant: str, conc: float) -> Optional[int]:
    """US EPA sub index for one pollutant. None if we cannot place the value."""
    if pollutant not in US_BREAKPOINTS:
        return None

    bps = US_BREAKPOINTS[pollutant]

    # the epa tables are written to a fixed precision and the rows do not touch:
    # pm2.5 runs [0.0, 9.0] then [9.1, 35.4]. truncating first, as the epa
    # instructs, is what stops a reading of 9.05 falling between two rows and
    # matching neither.
    if pollutant in ("pm2_5", "co"):
        conc = math.floor(conc * 10) / 10
    else:
        conc = int(conc)

    if conc < bps[0][0]:
        return 0

    for c_low, c_high, i_low, i_high in bps:
        if c_low <= conc <= c_high:
            return linear_interpolate(conc, (c_low, c_high, i_low, i_high))

    # off the top of the table. the index is capped rather than extrapolated.
    last_bp = bps[-1]
    if conc > last_bp[1]:
        return 500

    return None


def get_single_pollutant_aqi(pollutant: str, conc: float) -> Optional[int]:
    """Indian CPCB sub index for one pollutant, from the breakpoints json."""
    if pollutant not in AQI_BREAKPOINTS:
        return None

    bps = AQI_BREAKPOINTS[pollutant]

    if conc < bps[0][0]:
        return 0

    for c_low, c_high, i_low, i_high in bps:
        if c_low <= conc <= c_high:
            return linear_interpolate(conc, (c_low, c_high, i_low, i_high))

    if conc > bps[-1][1]:
        return 500

    # worth knowing: the cpcb rows are integers and do not touch either, pm2.5
    # runs [0, 30] then [31, 60]. a daily mean of 30.4 matches no row and falls
    # through to here, so the index comes back missing rather than wrong. the
    # us path above avoids this by truncating first.
    return None


def prepare_for_indian_aqi(pollutant: str, val_ugm3: float) -> float:
    """Put a value in the units the cpcb table expects. mg/m3 for co and ch4."""
    if pollutant in ["co", "ch4"]:
        return val_ugm3 / 1000.0

    return val_ugm3


def calculate_overall_aqi(
    pollutants_ugm3: Dict[str, float],
    zone_type: str = "default",
) -> Dict[str, Any]:
    """Build both indices from a bag of concentrations, keyed however the source named them."""
    aqi_details = {}
    us_aqi_details = {}
    concentrations_formatted = {}

    # upstream sources disagree on spelling, so every accepted name is mapped
    # onto one internal key.
    key_map = {
        "pm2.5": "pm2_5",
        "pm2_5": "pm2_5",
        "pm25": "pm2_5",
        "pm10": "pm10",
        "co": "co",
        "carbon_monoxide": "co",
        "no2": "no2",
        "nitrogen_dioxide": "no2",
        "so2": "so2",
        "sulphur_dioxide": "so2",
        "ch4": "ch4",
        "methane": "ch4",
    }

    for raw_key, val in pollutants_ugm3.items():
        k = raw_key.lower().strip()
        if k not in key_map:
            continue

        internal_key = key_map[k]

        # indian index. the concentration we report back is the one in cpcb
        # units, so the number on screen matches the index beside it.
        indian_unit_val = prepare_for_indian_aqi(internal_key, val)
        concentrations_formatted[internal_key] = round(indian_unit_val, 2)

        aqi_val = get_single_pollutant_aqi(internal_key, indian_unit_val)
        if aqi_val is not None:
            aqi_details[internal_key] = aqi_val

        # us index. particulates are already in the right units, gases are not.
        if internal_key in ["pm2_5", "pm10"]:
            us_val = get_us_aqi(internal_key, val)
            if us_val is not None:
                us_aqi_details[internal_key] = us_val

        elif internal_key in ["no2", "so2", "co"]:
            converted = _ugm3_to_ppb(internal_key, val)
            if converted is not None:
                us_val = get_us_aqi(internal_key, converted)
                if us_val is not None:
                    us_aqi_details[internal_key] = us_val

    overall_aqi = 0
    main_pollutant = "n/a"

    overall_us_aqi = 0
    us_main_pollutant = "n/a"

    # the index is the worst sub index, not an average, and the pollutant that
    # produced it is the one worth naming.
    if aqi_details:
        main_pollutant = max(aqi_details, key=aqi_details.get)
        overall_aqi = aqi_details[main_pollutant]

    if us_aqi_details:
        us_main_pollutant = max(us_aqi_details, key=us_aqi_details.get)
        overall_us_aqi = us_aqi_details[us_main_pollutant]

    return {
        "aqi": overall_aqi,
        "us_aqi": overall_us_aqi,
        "main_pollutant": main_pollutant,
        "us_main_pollutant": us_main_pollutant,
        "aqi_breakdown": aqi_details,
        "concentrations_us_units": concentrations_formatted,
        "zone_applied": zone_type,
    }
