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

# one PM2.5 correction for every airgradient sensor.
#
# airgradient lets each sensor pick its own correction, so the network ends up
# with the EPA formula on some and a batch correction that only applies in clean
# air on the rest. what a sensor is doing is decided from its calm readings,
# where airgradient's value and the raw reading line up closely in time. if
# those match the EPA formula worked out from raw PM2.5 and raw humidity,
# airgradient's values are kept, spikes included. otherwise the EPA value
# replaces them.

from typing import Iterable, Optional, Tuple

# how close airgradient's value has to be to the EPA value to count as the EPA
# formula, in ug/m3 and as a share of the EPA value, whichever is larger.
EPA_MATCH_ABSOLUTE = 1.0
EPA_MATCH_RELATIVE = 0.03

# raw PM2.5 under this counts as calm. above it, a stored raw value and
# airgradient's value can come from slightly different moments of a spike.
CALM_LIMIT = 200.0
REGIME_MIN_READINGS = 6

# bumped whenever the rules below change, so stored days decided under the old
# ones are decided again.
CALIBRATION_VERSION = 2

# the median of airgradient's value over the EPA value. sensors on EPA sit at
# 1.00 every day, sensors on the batch correction between 1.19 and 2.17.
REGIME_EPA_BAND = 0.1
REGIME_OTHER_BAND = 0.15


def epa_pm25(pm: float, rh: float) -> float:
    """EPA 2021 correction with the 2022 smoke extension, from raw PM2.5 and raw humidity."""
    if pm < 30:
        value = 0.524 * pm - 0.0862 * rh + 5.75
    elif pm < 50:
        x = pm / 20 - 1.5
        value = (0.786 * x + 0.524 * (1 - x)) * pm - 0.0862 * rh + 5.75
    elif pm < 210:
        value = 0.786 * pm - 0.0862 * rh + 5.75
    elif pm < 260:
        x = pm / 50 - 4.2
        value = (
            (0.69 * x + 0.786 * (1 - x)) * pm
            - 0.0862 * rh * (1 - x)
            + 2.966 * x
            + 5.75 * (1 - x)
            + 8.84e-4 * pm * pm * x
        )
    else:
        value = 2.966 + 0.69 * pm + 8.84e-4 * pm * pm

    return max(0.0, value)


def matches_epa(value: float, epa: float) -> bool:
    """Whether a corrected value is the EPA value, allowing for rounding and averaging."""
    return abs(value - epa) <= max(EPA_MATCH_ABSOLUTE, EPA_MATCH_RELATIVE * epa)


def sensor_uses_epa(
    readings: Iterable[Tuple[Optional[float], Optional[float], Optional[float]]],
) -> Optional[bool]:
    """Whether airgradient applied the EPA formula across a sensor's readings.

    Takes (airgradient value, raw PM2.5, raw humidity) for each reading and
    looks at the median of airgradient's value over the EPA value across the
    calm ones. Single readings can be off because the stored raw value and
    airgradient's value don't always come from the same moment, so the median
    is used rather than a count of exact matches. None when there are too few
    calm readings, or when the median is in between.
    """
    ratios = []

    for corrected, raw, humidity_raw in readings:
        if corrected is None or raw is None or humidity_raw is None:
            continue

        if raw >= CALM_LIMIT:
            continue

        epa = epa_pm25(raw, humidity_raw)
        if epa < 1:
            continue

        ratios.append(corrected / epa)

    if len(ratios) < REGIME_MIN_READINGS:
        return None

    ratios.sort()
    median = ratios[len(ratios) // 2]

    if abs(median - 1) <= REGIME_EPA_BAND:
        return True

    if abs(median - 1) >= REGIME_OTHER_BAND:
        return False

    return None


def calibrate_pm25(
    corrected: Optional[float],
    raw: Optional[float],
    humidity_raw: Optional[float],
    uses_epa: Optional[bool] = None,
) -> Optional[float]:
    """The PM2.5 to serve for one reading.

    uses_epa is what sensor_uses_epa decided for the sensor. airgradient's own
    value when it was applying EPA, or when there is no raw reading to work
    from, the EPA value when it wasn't. when it can't be told, the reading is
    checked on its own.
    """
    if raw is None or humidity_raw is None:
        if corrected is not None:
            return corrected
        return raw

    if uses_epa is True and corrected is not None:
        return corrected

    epa = epa_pm25(raw, humidity_raw)
    if uses_epa is None and corrected is not None and matches_epa(corrected, epa):
        return corrected

    return round(epa, 2)


def epa_pm25_sql(pm: str, rh: str, is_pg: bool) -> str:
    """epa_pm25 as a SQL expression over two column names, for recalibrating stored rows."""
    floor = "GREATEST" if is_pg else "MAX"
    return f"""{floor}(0.0, CASE
        WHEN {pm} < 30 THEN 0.524 * {pm} - 0.0862 * {rh} + 5.75
        WHEN {pm} < 50 THEN
            (0.786 * ({pm} / 20.0 - 1.5) + 0.524 * (1 - ({pm} / 20.0 - 1.5))) * {pm}
            - 0.0862 * {rh} + 5.75
        WHEN {pm} < 210 THEN 0.786 * {pm} - 0.0862 * {rh} + 5.75
        WHEN {pm} < 260 THEN
            (0.69 * ({pm} / 50.0 - 4.2) + 0.786 * (1 - ({pm} / 50.0 - 4.2))) * {pm}
            - 0.0862 * {rh} * (1 - ({pm} / 50.0 - 4.2))
            + 2.966 * ({pm} / 50.0 - 4.2)
            + 5.75 * (1 - ({pm} / 50.0 - 4.2))
            + 0.000884 * {pm} * {pm} * ({pm} / 50.0 - 4.2)
        ELSE 2.966 + 0.69 * {pm} + 0.000884 * {pm} * {pm}
    END)"""
