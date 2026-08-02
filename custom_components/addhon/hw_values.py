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


# --- what the appliance is DOING right now -----------------------------------
#
# The heat sources the appliance reports as currently running, ground-truthed on the real
# HP250M7C-F9 (the same attributes the compressor / electric-heater binary sensors read).
# Mapped to stable machine names so every consumer exposes a language-neutral value.
# Protection statuses (antifreeze, defrost) are deliberately NOT here: they are not
# heating the water.
HW_HEAT_SOURCES: tuple[tuple[str, str], ...] = (
    ("compressor", "compressorHeatingCurrentStatus"),
    ("electric_heater", "electricHeatingCurrentStatus"),
    ("aux_electric_heater", "auxElecHeatingStatus"),
    ("boiler", "boilerHeatingCurrentStatus"),
)

# Power flag in the cloud shadow. Read as a REPORTING, not as a capability: a device that
# says it is off is off whether or not this integration found a command to write it.
HW_POWER_ATTR = "onOffStatus"

# Action values. Literals rather than homeassistant.const.STATE_OFF/STATE_ON: this module
# is deliberately HA-free (see the module docstring) and "off" is a stable HA state name.
HW_ACTION_OFF = "off"
HW_ACTION_IDLE = "idle"
HW_ACTION_HEATING = "heating"
HW_ACTIONS: tuple[str, ...] = (HW_ACTION_OFF, HW_ACTION_IDLE, HW_ACTION_HEATING)

# Reported when more than one source runs at once (the classic compressor + resistance
# boost). A SCALAR value, not a list: Home Assistant only translates scalar values, and
# this integration keeps every user-facing string in translations/ -- joining labels in
# code would ship untranslated English. Per-source detail stays available as the
# individual binary sensors, which is the better handle for automations anyway.
HW_SOURCE_NONE = "none"
HW_SOURCE_MULTIPLE = "multiple"
HW_SOURCES: tuple[str, ...] = (
    (HW_SOURCE_NONE,) + tuple(name for name, _ in HW_HEAT_SOURCES) + (HW_SOURCE_MULTIPLE,)
)


def hw_running_sources(get_attr) -> list[str] | None:
    """Machine names of the heat sources reported as RUNNING.

    Returns None -- not an empty list -- when the device reports no heat-source status at
    all, so every caller can tell "nothing is running" apart from "this model does not
    say", and none of them has to invent a confident "not heating".
    """
    running: list[str] = []
    reported = False
    for name, attr in HW_HEAT_SOURCES:
        raw = get_attr(attr)
        if raw is None:
            continue
        reported = True
        if str(raw).strip() == "1":
            running.append(name)
    return running if reported else None


def hw_action(get_attr) -> str | None:
    """off / idle / heating, or None when the device reports no heat source.

    Power wins over the sources: an appliance that reports itself off is off, whatever a
    stale source flag still says.
    """
    running = hw_running_sources(get_attr)
    if running is None:
        return None
    raw = get_attr(HW_POWER_ATTR)
    if raw is not None and str(raw).strip() == "0":
        return HW_ACTION_OFF
    return HW_ACTION_HEATING if running else HW_ACTION_IDLE


def hw_heat_source(get_attr) -> str | None:
    """The single running source, `multiple`, `none`, or None when unreported."""
    running = hw_running_sources(get_attr)
    if running is None:
        return None
    if not running:
        return HW_SOURCE_NONE
    if len(running) == 1:
        return running[0]
    return HW_SOURCE_MULTIPLE
