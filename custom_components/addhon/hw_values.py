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


# --- scheduled vacation ------------------------------------------------------
#
# Ground truth: two diagnostics dumps of the real HP250M7C-F9 around a scheduled
# vacation window (grSetVacDate 2026-08-18 -> 2026-08-22). With the window still in
# the future machMode read 1; the day the window started it flipped to 4 -- while
# startProgram.program stayed "auto" (the scheduled holiday never touches the
# program) and the official app showed vacation ON. machMode is therefore the only
# signal that the device is ACTUALLY holidaying; the program enum only says what it
# will run when it is not.
HW_MACH_MODE_ATTR = "machMode"
HW_VACATION_MACH_MODE = "4"


def hw_vacation_active(raw) -> bool | None:
    """True while machMode reports the vacation hold; None when unreported."""
    if raw is None:
        return None
    return str(raw).strip() == HW_VACATION_MACH_MODE


# --- the appliance's own schedule --------------------------------------------
#
# Ground truth: full command + shadow dump of a real HP250M7C-F9 (2026-07-26 ->
# 2026-08-25, four captures). The appliance schedules itself in three independent ways,
# all of them reported as plain shadow attributes and all of them configured from the
# official app:
#
#   timingOnOffStatus + timingPowerOn/timingPowerOff   a daily power on/off timer
#   opp{1,2}Eco{Start,End}Time{1,2,3} + opp1EcoDays    up to 3 "cheap energy" windows
#                                                      per period group, with a day mask
#   silent{Start,End}Time{1,2}                         up to 2 quiet windows
#   sterilizationTime + sterilizationInterval          the anti-legionella cycle
#
# These are READ-ONLY here. Every one of them is a parameter of the `settings` command,
# which on this appliance is pinned to a single operation (see
# hon_commands.settings_write_blocked), so a write would be silently dropped -- exposing
# them as writable controls would be exposing controls that do nothing.
#
# The device spells "this slot is unused" as 00:00, on BOTH ends of the window: all six
# eco slots and both silent slots read 00:00 on every capture, with the features off.

# Unused-slot spelling, and the separator between the two ends of a window.
HW_TIME_UNSET = "00:00"
HW_WINDOW_SEPARATOR = "-"
HW_SCHEDULE_SEPARATOR = ", "

# Slot counts, as the parameter names enumerate them.
HW_ECO_GROUPS: tuple[int, ...] = (1, 2)
HW_ECO_SLOTS: tuple[int, ...] = (1, 2, 3)
HW_SILENT_SLOTS: tuple[int, ...] = (1, 2)

# The eco day mask, reported as hex ("7F" = seven bits = every day). Exposed RAW on
# purpose: only the all-days value has ever been observed, so which bit carries which
# weekday is not derivable from the evidence, and a decoded weekday list would assert an
# ordering nothing supports.
HW_ECO_DAYS_ATTR = "opp1EcoDays"

# The device's own weekday, ISO-numbered. Verified against the appliance's `date` field
# on all four captures (2026-07-26 Sun -> 7, 2026-08-16 Sun -> 7, 2026-08-18 Tue -> 2,
# 2026-08-25 Tue -> 2), which is what makes the 7-slot Day energy series addressable:
# the slot for today is index weekDay - 1.
HW_WEEKDAY_ATTR = "weekDay"


def hw_time(raw) -> str | None:
    """A device clock field normalised to "HH:MM", or None when it is not one.

    Strict on purpose: the same names appear on the `settings` COMMAND with numeric
    placeholder values (timingPowerOn is a range[0,1] there while the shadow reports
    "00:00"), so anything that is not an actual HH:MM reading is refused rather than
    rendered as a bogus time.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    hours, _, minutes = text.partition(":")
    if not minutes:
        return None
    try:
        hour = int(hours)
        minute = int(minutes)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def hw_window(start_raw, end_raw) -> str | None:
    """"HH:MM-HH:MM" for a CONFIGURED window, or None when the slot is unused.

    A slot whose two ends are equal is not a window: it is how the device spells an
    empty slot (00:00-00:00 on every capture), and a zero-length window is
    indistinguishable from one that never runs anyway.
    """
    start = hw_time(start_raw)
    end = hw_time(end_raw)
    if start is None or end is None or start == end:
        return None
    return f"{start}{HW_WINDOW_SEPARATOR}{end}"


def _hw_windows(get_attr, names) -> str | None:
    """The configured windows among `names` ((start_attr, end_attr) pairs), joined.

    None -- not an empty string -- when none of the slots is configured: the schedule
    then has no value to report, and an empty state would read as a window of zero
    length rather than as "nothing scheduled".
    """
    windows = [
        window
        for start_attr, end_attr in names
        if (window := hw_window(get_attr(start_attr), get_attr(end_attr))) is not None
    ]
    return HW_SCHEDULE_SEPARATOR.join(windows) if windows else None


def hw_eco_schedule(get_attr, group: int) -> str | None:
    """The "cheap energy" windows of period group `group` (1 or 2), joined."""
    return _hw_windows(
        get_attr,
        [
            (f"opp{group}EcoStartTime{slot}", f"opp{group}EcoEndTime{slot}")
            for slot in HW_ECO_SLOTS
        ],
    )


def hw_silent_schedule(get_attr) -> str | None:
    """The quiet-mode windows, joined."""
    return _hw_windows(
        get_attr,
        [
            (f"silentStartTime{slot}", f"silentEndTime{slot}")
            for slot in HW_SILENT_SLOTS
        ],
    )


def hw_weekday(get_attr) -> int | None:
    """The device's own ISO weekday (1 = Monday .. 7 = Sunday), or None.

    Read from the appliance rather than from the Home Assistant host: the daily counters
    roll over in the APPLIANCE's timezone. Falls back to its `date` field, which is
    reported next to the series and carries the same clock, and never to the host's --
    a wrong index would silently attribute one day's energy to another.
    """
    raw = get_attr(HW_WEEKDAY_ATTR)
    if raw is not None:
        try:
            day = int(str(raw).strip())
        except (TypeError, ValueError):
            day = 0
        if 1 <= day <= 7:
            return day
    raw_date = get_attr("date")
    if raw_date is None:
        return None
    try:
        from datetime import date as _date

        year, month, day_of_month = (int(part) for part in str(raw_date).split("-"))
        return _date(year, month, day_of_month).isoweekday()
    except (TypeError, ValueError):
        return None


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
