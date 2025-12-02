from typing import Dict, Any, Optional, Tuple
from config import AQI_BREAKPOINTS

def linear_interpolate(c: float, bp: Tuple[float, float, int, int]) -> int:
    c_lo, c_hi, i_lo, i_hi = bp
    if c_hi - c_lo == 0:
        return i_lo
    val = ((i_hi - i_lo) / (c_hi - c_lo)) * (c - c_lo) + i_lo
    return int(round(val))

def get_single_pollutant_aqi(pollutant: str, conc: float) -> Optional[int]:
    if pollutant not in AQI_BREAKPOINTS:
        return None
    bps = AQI_BREAKPOINTS[pollutant]
    if conc < bps[0][0]:
        return 0
    for bp in bps:
        if bp[0] <= conc <= bp[1]:
            return linear_interpolate(conc, bp)
    last_bp = bps[-1]
    if conc > last_bp[1]:
        return last_bp[3]
    return None

def convert_to_us_units(pollutant: str, val_ugm3: float, temp_k: float = 298.15) -> float:
    if pollutant == "co":
        base = val_ugm3 / 1145.0
        return base * (temp_k / 298.15)
    elif pollutant == "no2":
        base = val_ugm3 / 1.88
        return base * (temp_k / 298.15)
    elif pollutant == "so2":
        base = val_ugm3 / 2.62
        return base * (temp_k / 298.15)
    elif pollutant == "o3":
        base = val_ugm3 / 1.96
        return base * (temp_k / 298.15)
    return val_ugm3

def calculate_overall_aqi(pollutants_ugm3: Dict[str, float], temp_k: float = 298.15) -> Dict[str, Any]:
    aqi_details = {}
    concentrations_formatted = {}

    key_map = {
        "pm2.5": "pm2_5", "pm2_5": "pm2_5", "pm25": "pm2_5",
        "pm10": "pm10",
        "co": "co",
        "no2": "no2",
        "so2": "so2",
        "o3": "o3", "ozone": "o3"
    }

    for raw_key, val in pollutants_ugm3.items():
        k = raw_key.lower()
        if k in key_map:
            internal_key = key_map[k]
            converted_val = convert_to_us_units(internal_key, val, temp_k)
            concentrations_formatted[internal_key] = round(converted_val, 2)
            aqi_val = get_single_pollutant_aqi(internal_key, converted_val)
            if aqi_val is not None:
                aqi_details[internal_key] = aqi_val

    overall_aqi = 0
    main_pollutant = "n/a"

    if aqi_details:
        main_pollutant = max(aqi_details, key=aqi_details.get)
        overall_aqi = aqi_details[main_pollutant]

    return {
        "us_aqi": overall_aqi,
        "main_pollutant": main_pollutant,
        "aqi_breakdown": aqi_details,
        "concentrations_us_units": concentrations_formatted,
        "concentrations_raw_ugm3": pollutants_ugm3
    }