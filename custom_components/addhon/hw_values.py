# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Derived values for heat pump water heaters (HW), shared across platforms.

Home Assistant is deliberately NOT imported here: this module holds pure derivations of
device readings, so it stays importable from any platform without dragging in (or
stubbing) the entity stack. It exists so the sensor and the water_heater entity cannot
report different numbers for the same reading -- there is one calibration, in one place.
"""
from __future__ import annotations

# `remainingWaterLevel` is a 0..12 gauge, NOT a percentage: the real HP250M7C-F9 reports
# 12 while the official app shows 100% (one-point calibration; 0 -> 0%).
HW_WATER_LEVEL_ATTR = "remainingWaterLevel"
HW_WATER_LEVEL_FULL = 12.0


def hw_water_level(raw) -> float | None:
    """`remainingWaterLevel` as a 0..100 percentage, or None if absent/non-numeric.

    Capped at 100 so a gauge reading above the calibration point cannot report a
    nonsensical percentage; a reading below 0 is passed through rather than clamped,
    because it would mean the calibration itself is wrong and hiding that helps nobody.
    """
    if raw is None:
        return None
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return min(100.0, round(value * 100.0 / HW_WATER_LEVEL_FULL, 1))
