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
# air on the rest. every reading is checked against the EPA formula worked out
# from its own raw PM2.5 and raw humidity. if airgradient's value already
# matches, it is kept, otherwise the EPA value replaces it.

from typing import Optional

# how close airgradient's value has to be to the EPA value to count as the EPA
# formula, in ug/m3 and as a share of the EPA value, whichever is larger.
EPA_MATCH_ABSOLUTE = 1.0
EPA_MATCH_RELATIVE = 0.03


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


def calibrate_pm25(
    corrected: Optional[float],
    raw: Optional[float],
    humidity_raw: Optional[float],
) -> Optional[float]:
    """The PM2.5 to serve for one reading.

    airgradient's own value when it already applied the EPA formula, or when
    there is no raw reading to work from. the EPA value otherwise.
    """
    if raw is None or humidity_raw is None:
        if corrected is not None:
            return corrected
        return raw

    epa = epa_pm25(raw, humidity_raw)
    if corrected is not None and matches_epa(corrected, epa):
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
