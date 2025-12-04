import math
from typing import Dict, Any, Optional, Tuple, Union
from config import AQI_BREAKPOINTS

def linear_interpolate(c: float, bp: Tuple[float, float, int, int]) -> int:
    c_lo, c_hi, i_lo, i_hi = bp
    if c_hi == c_lo:
        return i_lo
    val = ((i_hi - i_lo) / (c_hi - c_lo)) * (c - c_lo) + i_lo
    return int(val)

def get_single_pollutant_aqi(pollutant: str, conc: float) -> Optional[int]:
    if pollutant not in AQI_BREAKPOINTS:
        return None

    bps = AQI_BREAKPOINTS[pollutant]
    
    if conc < bps[0][0]:
        return 0

    for c_low, c_high, i_low, i_high in bps:
        if c_low <= conc <= c_high:
            return linear_interpolate(conc, (c_low, c_high, i_low, i_high))

    last_bp = bps[-1]
    if conc > last_bp[1]:
        return 500

    return None

def prepare_for_indian_aqi(pollutant: str, val_ugm3: float) -> float:
    if pollutant == "co":
        return val_ugm3 / 1000.0
    return val_ugm3

def calculate_overall_aqi(pollutants_ugm3: Dict[str, float], zone_type: str = "default") -> Dict[str, Any]:
    aqi_details = {}
    concentrations_formatted = {}

    key_map = {
        "pm2.5": "pm2_5", "pm2_5": "pm2_5", "pm25": "pm2_5",
        "pm10": "pm10",
        "co": "co", "carbon_monoxide": "co",
        "no2": "no2", "nitrogen_dioxide": "no2",
        "so2": "so2", "sulphur_dioxide": "so2",
        "o3": "o3", "ozone": "o3"
    }

    for raw_key, val in pollutants_ugm3.items():
        k = raw_key.lower().strip()
        if k not in key_map:
            continue
    
        internal_key = key_map[k]
        indian_unit_val = prepare_for_indian_aqi(internal_key, val)

        concentrations_formatted[internal_key] = round(indian_unit_val, 2)

        aqi_val = get_single_pollutant_aqi(internal_key, indian_unit_val)
        if aqi_val is not None:
            aqi_details[internal_key] = aqi_val

    overall_aqi = 0
    main_pollutant = "n/a"

    if aqi_details:
        main_pollutant = max(aqi_details, key=aqi_details.get)
        overall_aqi = aqi_details[main_pollutant]

    return {
        "aqi": overall_aqi,
        "main_pollutant": main_pollutant,
        "aqi_breakdown": aqi_details,
        "concentrations_us_units": concentrations_formatted,
        "concentrations_raw_ugm3": pollutants_ugm3,
        "zone_applied": zone_type
    }