# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Water heater entity for Haier hOn heat pump water heaters (HW) and water heaters (WH).

Home Assistant has a DEDICATED `water_heater` domain for this appliance class; a
`climate` entity would model it as a room thermostat (HVAC modes, exported to
HomeKit/Alexa/Google as a house thermostat), which it is not. This platform maps the
device onto every control the domain can express -- there is no fifth feature to add:

    TARGET_TEMPERATURE  tempSel
    OPERATION_MODE      startProgram program enum (auto / eco / elec / vac)
    ON_OFF              onOffStatus
    AWAY_MODE           the `vac` (holiday) program

(TARGET_TEMPERATURE_RANGE is for dual high/low setpoints and does not apply: the
device has a single target.)

Everything is CAPABILITY-GATED on the live runtime schema, like every other control in
this integration: each feature bit is set only when the device actually exposes the
parameter, and the entity itself is created only when a writable setpoint is found. A
plain WH that reports temperature read-only therefore produces NO entity rather than a
control that would silently fail.

WRITE PATHS (ground-truthed on a real HP250M7C-F9, see number.py / select.py):
- the setpoint is resolved on `startProgram` FIRST, because a tempSel sent through the
  HW `settings` command is silently ignored (live-verified: the device reverted the
  setpoint on the next poll) while the app writes mode+temperature via startProgram;
- the operating mode is a startProgram PROGRAM, sent via async_send_program (swap-aware);
- EVERY startProgram write re-asserts the running program (v5.23.1). The program is
  carried by the command CATEGORY, not by a payload parameter: api.send_command derives
  `programName` from the active category's own name, and the appliance's command history
  shows the transmitted parameters are only machMode/onOffStatus/tempSel. So a write sent
  on whatever category happens to be active tells the appliance to run THAT program --
  and after a restart the active one is the schema's first (`auto`), because the loader
  can only recover the category from a `program`/`category` key these payloads do not
  carry. Selecting the reported program first makes the envelope match what the appliance
  is actually running, which is also the shape the app sends;
- POWER is resolved the same way, on `startProgram` FIRST (v5.22.0). It used to go
  through `settings`, and did nothing at all: that command declares `operationName` as a
  MANDATORY fixed parameter pinned to "grSetVacDate" -- identical across four diagnostics
  dumps a month apart, so it is the cloud's command definition and not a leftover of the
  last app action -- and `command.send()` transmits the whole parameter group, so the
  appliance reads every settings write as "set the vacation dates" and drops the rest.
  That is the same trap the setpoint fell into in v5.10, and the dumps show it: the
  vacation window written from Home Assistant reached the device while onOffStatus stayed
  1 through every capture. `startProgram` carries onOffStatus as a mandatory parameter of
  its own, which makes it the only channel on this appliance that can express power. Where
  it is NOT there, `settings` is still tried -- and refused up front when
  settings_write_blocked says the pin would swallow it, so the entity offers no `off` it
  cannot deliver.

SINGLE SURFACE since v5.21.0: this entity is the ONLY handle for the main setpoint and
the operating mode. The parallel `number.target_temp` and `select.hw_mode` read and
wrote the same parameters and were retired as duplicates (their registry entries are
cleaned up by __init__._remove_legacy_entities). The remaining number/switch entities
stay because they cover what the water_heater domain has NO slot for (boost, silent,
child lock, sterilization, per-mode setpoints) -- not because they duplicate this one.
"""
from __future__ import annotations

import logging

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .const import APPLIANCE_HW, APPLIANCE_WH, DOMAIN
from .debug_utils import command_names, redact_id
from .hon_commands import (
    async_send_command,
    find_settings_param,
    param_range,
    settings_write_blocked,
    snap_to_range,
)
from .hw_values import (
    HW_MACH_MODE_ATTR,
    HW_POWER_ATTR,
    HW_WATER_LEVEL_ATTR,
    hw_action,
    hw_heat_source,
    hw_vacation_active,
    hw_water_level,
)
from .program_options import (
    STARTPROGRAM_COMMAND,
    async_send_program,
    startprogram_machmode_map,
    startprogram_program_codes,
    startprogram_program_param,
)

_LOGGER = logging.getLogger(__name__)

# Appliance types evaluated by this platform. HW is the ground-truthed one; WH (plain
# water heater) is included so a model that DOES expose a writable setpoint gets the
# entity, and one that does not simply fails the gate.
WATER_HEATER_TYPES = (APPLIANCE_HW, APPLIANCE_WH)

# Setpoint parameter and the commands searched for it, IN ORDER. startProgram comes
# first on purpose (see the module docstring): on the real HP250M7C-F9 tempSel also
# exists on `settings`, where writing it is silently dropped.
HW_TEMP_PARAM = "tempSel"
HW_TEMP_COMMANDS = ("startProgram", "settings", "setParameters")

# Power parameter. Same key the shared helpers read for the action, so the entity and the
# sensors cannot disagree on power.
HW_ON_OFF_PARAM = HW_POWER_ATTR

# Commands searched for the power parameter, IN ORDER -- startProgram first, for the
# reason spelled out in the module docstring. Kept as its own tuple rather than reusing
# HW_TEMP_COMMANDS: they happen to coincide today, and a future divergence in one must
# not silently move the other.
HW_ON_OFF_COMMANDS = ("startProgram", "settings", "setParameters")

# Current water temperature in the cloud shadow (same attribute the `water_temp` sensor
# reads).
HW_CURRENT_TEMP_ATTR = "temp"

# Device program code -> Home Assistant's STANDARD water_heater operation state.
#
# Load-bearing for the UI, not cosmetics: the frontend's mode picker (tile feature and
# more-info) resolves its icons from a HARDCODED map of the standard states -- it does NOT
# read icons.json -- so a model-specific code renders as an anonymous dot. `eco` is
# already standard; `auto` is the mode where the heat pump leads (with the resistance as
# backup) and `elec` is resistance-only.
#
# `vac` has NO standard equivalent: HA models holiday as the orthogonal away toggle, so it
# stays as the raw device code and is additionally exposed through away_mode. Any code not
# listed here passes through unchanged, so another model's modes still work.
HW_MODE_TO_HA: dict[str, str] = {
    "auto": "heat_pump",
    "eco": "eco",
    "elec": "electric",
    "electric": "electric",
}

# The holiday/vacation program. On the device it is an ordinary startProgram mode, but
# HA models "away" as its own orthogonal toggle, so it is exposed BOTH ways: as an
# operation-mode option and as the away switch. Both read and write the same program, so
# they can never disagree.
#
# The program is NOT the only way the device holidays: a window scheduled by dates in
# the app (grSetVacDate, see date.py) leaves the program on whatever it was and flips
# machMode to the vacation hold instead (see hw_values). The read side therefore also
# honours machMode, or an actively-holidaying device would report Auto / away off.
HW_AWAY_CODE = "vac"

# Volatile per-coordinator store: the operating mode active before away was switched on,
# so turning away OFF can restore it instead of guessing. Survives coordinator refreshes
# (kept on the coordinator object, not in coordinator.data), not a HA restart -- the
# fallback below covers that.
HW_LAST_MODE_STORE = "hw_last_mode"

# Used only when the device exposes no range on its tempSel parameter. Mirrors the real
# HP250M7C-F9 schema dump (range[35,75,1]); the LIVE range is preferred on every read.
HW_TEMP_FALLBACK_RANGE = (35.0, 75.0, 1.0)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one water_heater entity per appliance that exposes a writable setpoint."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities: list[HonWaterHeater] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        app_type = data.get("type")
        if app_type not in WATER_HEATER_TYPES:
            continue
        appliance = data.get("appliance")
        if not HonWaterHeater.supports_appliance(appliance):
            _LOGGER.debug(
                "WaterHeater debug: no entity for '%s' id=%s type=%s; needs a writable "
                "%s in one of %s. commands=%s",
                data.get("name"),
                redact_id(appliance_id),
                app_type,
                HW_TEMP_PARAM,
                HW_TEMP_COMMANDS,
                command_names(appliance),
            )
            continue
        entities.append(HonWaterHeater(coordinator, appliance_id, client))
        _LOGGER.info(
            "Added water heater entity: id=%s type=%s", redact_id(appliance_id), app_type
        )
    async_add_entities(entities)


class HonWaterHeater(HonBaseEntity, WaterHeaterEntity):
    """Haier hOn water heater: target temperature, operating mode, power and away."""

    # The device's main entity: it takes the device name (has_entity_name + name None).
    # The translation_key is still honoured for the STATE (operation mode) labels AND
    # for the per-state icons in icons.json. Deliberately NO _attr_icon: an entity that
    # provides its own icon overrides the icon translations, which would pin one static
    # boiler icon instead of letting it follow the running mode.
    _attr_name = None
    _attr_translation_key = "water_heater"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_water_heater"
        appliance = self._appliance

        # --- target temperature (the gate: no setpoint -> no entity) ---------------
        resolved = find_settings_param(appliance, HW_TEMP_PARAM, HW_TEMP_COMMANDS)
        self._temp_command, self._temp_param = resolved if resolved else (None, None)
        # Range snapshot used only as a fallback; the live bounds are re-read from the
        # parameter on every access (the engine rules can move them at runtime).
        self._temp_fallback_range = (
            param_range(self._temp_param) if self._temp_param is not None else None
        ) or HW_TEMP_FALLBACK_RANGE

        # --- power ----------------------------------------------------------------
        on_off = find_settings_param(appliance, HW_ON_OFF_PARAM, HW_ON_OFF_COMMANDS)
        self._on_off_command = on_off[0] if on_off is not None else None
        # A power parameter that only exists on a settings command PINNED to another
        # operation cannot be written: the appliance would accept the payload and ignore
        # it. Drop the capability rather than offer an `off` that does nothing -- the same
        # rule the setpoint gate applies (no writable parameter -> no control).
        if self._on_off_command is not None:
            blocked_by = settings_write_blocked(
                appliance, HW_ON_OFF_PARAM, self._on_off_command
            )
            if blocked_by is not None:
                _LOGGER.info(
                    "WaterHeater: no power control for id=%s -- '%s' is pinned to "
                    "operation '%s', which would swallow %s",
                    redact_id(appliance_id),
                    self._on_off_command,
                    blocked_by,
                    HW_ON_OFF_PARAM,
                )
                self._on_off_command = None

        # --- operating modes ------------------------------------------------------
        program = startprogram_program_param(appliance)
        self._program_param_name = program[1] if program is not None else None
        self._mode_codes = startprogram_program_codes(appliance)
        # The device's own spelling of the holiday program (never assume lowercase).
        self._away_code = next(
            (code for code in self._mode_codes if code.lower() == HW_AWAY_CODE), None
        )
        self._normal_codes = [code for code in self._mode_codes if code != self._away_code]
        # Device code <-> HA operation state, resolved once. An ambiguous mapping (two
        # device codes claiming the same standard state, or one colliding with the
        # synthetic on/off options) falls back to the RAW codes for every mode: device
        # codes are unique by construction, so the round-trip can never mis-address a
        # program. Correctness first, icons second.
        mapped = {code: HW_MODE_TO_HA.get(code.lower(), code) for code in self._mode_codes}
        names = list(mapped.values())
        if len(set(names)) != len(names) or STATE_OFF in names or STATE_ON in names:
            _LOGGER.debug(
                "WaterHeater debug: ambiguous mode mapping %s, using raw device codes",
                mapped,
            )
            mapped = {code: code for code in self._mode_codes}
        self._code_to_ha = mapped
        self._ha_to_code = {ha: code for code, ha in mapped.items()}

        features = WaterHeaterEntityFeature(0)
        if self._temp_param is not None:
            features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if self._on_off_command is not None:
            features |= WaterHeaterEntityFeature.ON_OFF
        # Away is offered only when the holiday program exists AND there is another mode
        # to come back to, so turning it off can never dead-end.
        if self._away_code is not None and self._normal_codes:
            features |= WaterHeaterEntityFeature.AWAY_MODE

        if self._mode_codes:
            # "off" is a synthetic option backed by onOffStatus: the device has no
            # stopProgram, one program is always active (see select.py's HW note).
            has_power = self._on_off_command is not None
            options = ([STATE_OFF] if has_power else []) + [
                self._code_to_ha[code] for code in self._mode_codes
            ]
        elif self._on_off_command is not None:
            # No program enum: the only operating states the device knows are on/off.
            options = [STATE_OFF, STATE_ON]
        else:
            options = []
        self._attr_operation_list = options or None
        if options:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        self._attr_supported_features = features

        _LOGGER.debug(
            "WaterHeater debug: initialized '%s' id=%s temp_cmd=%s range=%s "
            "on_off_cmd=%s modes=%s away=%s features=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            self._temp_command,
            self._live_temp_range,
            self._on_off_command,
            self._mode_codes,
            self._away_code,
            int(features),
        )

    @classmethod
    def supports_appliance(cls, appliance) -> bool:
        """True if the device exposes a WRITABLE setpoint in one of HW_TEMP_COMMANDS.

        The setpoint is what makes a water_heater entity worth having; modes, power and
        away are all optional feature bits on top of it.
        """
        return find_settings_param(appliance, HW_TEMP_PARAM, HW_TEMP_COMMANDS) is not None

    # --- temperature -------------------------------------------------------------

    @property
    def _live_temp_param(self):
        """The CURRENT runtime tempSel parameter, or None.

        Re-resolved on every access rather than read off the __init__ snapshot: selecting
        a program REPLACES appliance.commands["startProgram"] with the chosen category's
        own command object (see async_send_program), so the snapshot would keep reporting
        the pre-swap category's parameter. Falls back to the snapshot.
        """
        resolved = find_settings_param(self._appliance, HW_TEMP_PARAM, HW_TEMP_COMMANDS)
        if resolved is not None:
            return resolved[1]
        return self._temp_param

    @property
    def _device_temp_range(self) -> tuple[float, float, float] | None:
        """(min, max, step) as the DEVICE declares it, or None when tempSel carries no
        range metadata. Kept apart from _live_temp_range on purpose: the write path may
        only snap a setpoint onto a grid the device actually declared, never onto the
        fallback below (a UI guess -- rounding to it could drop a value the device would
        have accepted)."""
        param = self._live_temp_param
        return param_range(param) if param is not None else None

    @property
    def _live_temp_range(self) -> tuple[float, float, float]:
        """(min, max, step) for the UI: the device's own range, else the static fallback,
        so a device that errored on load still offers a sane control."""
        return self._device_temp_range or self._temp_fallback_range

    @property
    def min_temp(self) -> float:
        return self._live_temp_range[0]

    @property
    def max_temp(self) -> float:
        return self._live_temp_range[1]

    @property
    def target_temperature_step(self) -> float:
        return self._live_temp_range[2]

    @property
    def current_temperature(self) -> float | None:
        return self._float_attr(HW_CURRENT_TEMP_ATTR)

    @property
    def target_temperature(self) -> float | None:
        return self._float_attr(HW_TEMP_PARAM)

    # --- what the appliance is DOING right now -----------------------------------

    @property
    def extra_state_attributes(self) -> dict | None:
        """`action` (off / idle / heating), `heat_source` and `hot_water_level`.

        The water_heater domain has NO equivalent of climate's hvac_action: the entity
        state is the SELECTED MODE, not what the appliance is doing. So this is exposed
        as attributes rather than folded into current_operation -- a synthetic "heating"
        state would overwrite the mode AND put a value in the state machine that is not a
        member of operation_list, leaving HA's mode picker with nothing selected.

        `action` and `heat_source` are SCALAR machine keys with `state_attributes`
        translations, so the frontend renders them as proper localized labels ("Heating",
        "Compressor") in a tile card's state content. A list would not: HA translates
        scalar attribute values only, and building a joined label in code would ship
        untranslated English.

        `hot_water_level` is the same 0..100 percentage the `hot_water_level` sensor
        reports, from the SAME calibration helper (hw_values), so the two can never
        disagree. It is a plain number: attributes carry no unit, so the card shows the
        figure without a `%` -- the sensor entity is the one to use where the unit matters.

        Every key is independently capability-gated: a reading the device does not report
        is omitted rather than guessed, and a device that reports none of them gets no
        attributes at all rather than a confident "not heating".

        These stay even though `sensor.<device>_heating_status` and
        `sensor.<device>_heat_source` now expose the same two readings as entities: the
        sensors are the GRAPHABLE handles (an attribute is recorded but no native card
        plots one, and the more-info history chart is single-entity by construction),
        while the attributes are what a tile card bound to THIS entity can put in its
        state content. Both call the same hw_values helpers, so they cannot drift.
        """
        attrs: dict = {}
        action = hw_action(self._get_attr)
        if action is not None:
            attrs["action"] = action
            attrs["heat_source"] = hw_heat_source(self._get_attr)
        level = hw_water_level(self._get_attr(HW_WATER_LEVEL_ATTR))
        if level is not None:
            attrs["hot_water_level"] = level
        return attrs or None

    def _float_attr(self, key: str) -> float | None:
        """Shadow attribute as a float, or None when absent/non-numeric (never a guess)."""
        raw = self._get_attr(key)
        if raw is None:
            return None
        try:
            return float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            _LOGGER.debug("WaterHeater debug: '%s' not numeric raw=%r", key, raw)
            return None

    async def async_set_temperature(self, **kwargs) -> None:
        """Write the target temperature through the resolved command."""
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None or self._temp_command is None:
            return
        # Snap onto the device's own min/max/step grid FIRST. HA does not enforce the
        # step: its dial adds the step to the entity state, so a shadow that reports an
        # off-grid setpoint (a live HP250M7C-F9 reported tempSel 59.2 on range[35,75,1])
        # turns every press into an off-grid request the range setter refuses, leaving
        # the user with "Allowed: min 35 max 75 step 1 But was: 60.2" and a setpoint that
        # never moves. The nearest grid point is the setpoint the user asked for.
        requested = float(value)
        number = snap_to_range(requested, self._device_temp_range, self._live_temp_param)
        if number != requested:
            _LOGGER.debug(
                "WaterHeater debug: setpoint %s snapped to %s on range %s id=%s",
                requested,
                number,
                self._device_temp_range,
                redact_id(self._appliance_id),
            )
        # ALWAYS send a string, and a clean one: the client's str_to_float does
        # int(string) and catches only ValueError, so a fractional float (55.5) would be
        # truncated to 55 WITHOUT error, while "55.5" stays 55.5 and the range setter
        # validates the step. An integer serializes as "55", never "55.0" (an enum/range
        # setter rejects the latter).
        send_value = str(int(number)) if number.is_integer() else str(number)
        what = f"{HW_TEMP_PARAM}={send_value}"
        if self._temp_command == STARTPROGRAM_COMMAND:
            # Same rule as the power write: the setpoint travels in an envelope that also
            # names a program, so it must name the RUNNING one.
            await self._async_write_start_program({HW_TEMP_PARAM: send_value}, what)
        else:
            await self._async_send({HW_TEMP_PARAM: send_value}, self._temp_command, what)
        await self._async_request_command_refresh()

    # --- power -------------------------------------------------------------------

    @property
    def _is_off(self) -> bool:
        """True only when the device REPORTS power off; an absent reading is not off."""
        raw = self._get_attr(HW_ON_OFF_PARAM)
        return raw is not None and str(raw).strip() == "0"

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write_power("1")
        await self._async_request_command_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write_power("0")
        await self._async_request_command_refresh()

    @property
    def _power_on_start_program(self) -> bool:
        """True when power rides on the SAME command a program start uses.

        Where it does, powering on before a program change must not be a second command:
        both values belong to one startProgram envelope, which is also the shape the
        official app sends (its command history carries machMode + onOffStatus + tempSel
        in a single startProgram).
        """
        return self._on_off_command == STARTPROGRAM_COMMAND

    async def _async_write_power(self, value: str) -> None:
        """Send onOffStatus WITHOUT refreshing (the callers batch the refresh)."""
        if self._on_off_command is None:
            return
        what = f"{HW_ON_OFF_PARAM}={value}"
        if self._power_on_start_program:
            await self._async_write_start_program({HW_ON_OFF_PARAM: value}, what)
            return
        await self._async_send({HW_ON_OFF_PARAM: value}, self._on_off_command, what)

    async def _async_write_start_program(self, params: dict[str, str], what: str) -> None:
        """Apply `params` to startProgram and send it, RE-ASSERTING the running program.

        See the module docstring: the program rides on the command CATEGORY, so sending
        on the wrong one silently changes the appliance's mode. Selecting the mode the
        appliance REPORTS costs nothing (it is already running it) and makes the envelope
        honest.

        Falls back to a plain send when the mode cannot be read or the device exposes no
        program enum -- no worse than before, and never a guessed program.
        """
        mode = self._live_mode
        if mode is None or self._program_param_name is None:
            _LOGGER.debug(
                "WaterHeater debug: %s on the active startProgram category (mode "
                "unknown) id=%s",
                what,
                redact_id(self._appliance_id),
            )
            await self._async_send(params, STARTPROGRAM_COMMAND, what)
            return
        _LOGGER.debug(
            "WaterHeater debug: %s on startProgram, re-asserting program '%s' id=%s",
            what,
            mode,
            redact_id(self._appliance_id),
        )
        await self._async_send_mode(mode, params)

    # --- operating mode ----------------------------------------------------------

    @property
    def _live_mode(self) -> str | None:
        """The active program, matched against the codes the device offers.

        Reads the poll-refreshed cloud shadow first and falls back to the value the
        command carries (recovered at command load). Double-gated against the offered
        codes: an unrecognized reading reports unknown rather than a guessed mode.
        """
        if not self._mode_codes or self._program_param_name is None:
            return None
        by_lower = {code.lower(): code for code in self._mode_codes}
        raw = self._get_attr(f"startProgram.{self._program_param_name}")
        if raw is None:
            resolved = startprogram_program_param(self._appliance)
            if resolved is not None:
                raw = getattr(resolved[0], "value", None)
        if raw is None:
            return None
        token = str(raw).strip().lower()
        return by_lower.get(token) or by_lower.get(token.rsplit(".", 1)[-1])

    @property
    def _running_mode(self) -> str | None:
        """The program the appliance says it is RUNNING, or None when it does not say.

        `machMode` is that report, and the mapping is read from the DEVICE SCHEMA: each
        startProgram category declares its own machMode (HP250M7C-F9: auto 1, eco 2,
        elec 3, vac 4), and the appliance's accepted commands carry exactly that pairing.
        Nothing is hard-coded -- a model that numbers its modes differently maps itself,
        and one that exposes no per-program categories simply reports None here.

        This is the reading that differs from `_live_mode`: the program enum is what the
        appliance is CONFIGURED to run, machMode is what it is running right now. A
        holiday window scheduled by dates moves only the second (live-verified around
        2026-08-18: machMode 1 -> 4 while the enum stayed `auto`).
        """
        raw = self._get_attr(HW_MACH_MODE_ATTR)
        if raw is None:
            return None
        mapping = startprogram_machmode_map(self._appliance)
        if not mapping:
            return None
        code = mapping.get(str(raw).strip())
        # Double-gated against the offered codes, like _live_mode: a category the program
        # enum does not list is not a mode this entity can report.
        return code if code in self._mode_codes else None

    @property
    def _vacation_hold(self) -> bool:
        """True while the appliance is RUNNING the holiday program, whether it was asked
        to (the vac program) or entered it by itself (a window scheduled by dates, which
        never touches the program enum -- live-verified on the HP250M7C-F9).

        Prefers the schema-derived machMode map; falls back to the hw_values constant,
        which is the same value read off that schema (the vac category declares
        machMode 4) and is what the binary sensor uses, so the two cannot disagree.
        """
        if self._away_code is not None:
            running = self._running_mode
            if running is not None:
                return running == self._away_code
        return hw_vacation_active(self._get_attr(HW_MACH_MODE_ATTR)) is True

    @property
    def current_operation(self) -> str | None:
        # Power wins: a device that is off is off, whichever program it would resume.
        if self._on_off_command is not None and self._is_off:
            return STATE_OFF
        if self._mode_codes:
            # An active vacation hold outranks the selected program: the device is
            # holidaying whatever the program says it will run afterwards. Reported
            # as the vac option (a member of operation_list) so the mode picker
            # keeps a valid selection; gated on _away_code so a model without a vac
            # program can never report an option it does not offer.
            if self._away_code is not None and self._vacation_hold:
                return self._code_to_ha.get(self._away_code, self._away_code)
            # What the appliance says it is RUNNING wins over what it is configured to
            # run: the two differ whenever it switches by itself, and the state should
            # describe the appliance rather than the setting. Falls back to the program
            # enum where machMode carries no readable mapping.
            #
            # Reported under HA's standard name so the frontend's built-in mode icons
            # apply; the device codes stay for the send path.
            mode = self._running_mode or self._live_mode
            return self._code_to_ha.get(mode, mode) if mode is not None else None
        # No program enum: on/off is the whole operating state (see __init__).
        if self._on_off_command is not None:
            return STATE_ON
        return None

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Switch program (or power, for the synthetic on/off options)."""
        options = self._attr_operation_list or []
        if operation_mode not in options:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_found",
                translation_placeholders={"program": operation_mode},
            )
        if operation_mode == STATE_OFF:
            await self.async_turn_off()
            return
        if operation_mode == STATE_ON:
            await self.async_turn_on()
            return
        # Back to the DEVICE's own program code: the option the user picked is HA's
        # standard name (see HW_MODE_TO_HA), which startProgram would not accept.
        await self._async_apply_mode(self._ha_to_code.get(operation_mode, operation_mode))

    async def _async_apply_mode(self, code: str) -> None:
        """Start the device program `code` (a raw device code, not an HA state)."""
        # Selecting a program on a powered-off device would leave it off (startProgram
        # does not imply power on), so power it back up first -- only when the device
        # actually reports itself off. When power lives on startProgram it rides along in
        # the SAME send instead of costing a second command.
        power_on = self._on_off_command is not None and self._is_off
        extra: dict[str, str] | None = None
        if power_on:
            _LOGGER.debug(
                "WaterHeater debug: powering on before program '%s' id=%s (same_command=%s)",
                code,
                redact_id(self._appliance_id),
                self._power_on_start_program,
            )
            if self._power_on_start_program:
                extra = {HW_ON_OFF_PARAM: "1"}
            else:
                await self._async_write_power("1")
        await self._async_send_mode(code, extra)
        await self._async_request_command_refresh()

    async def _async_send_mode(
        self, code: str, extra_params: dict[str, str] | None = None
    ) -> None:
        """Send the program via the swap-aware startProgram sender (no refresh)."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            _LOGGER.info(
                "WaterHeater: mode '%s' -> startProgram id=%s",
                code,
                redact_id(self._appliance_id),
            )
            await async_send_program(
                self.hass, client, appliance, code, extra_params
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("WaterHeater: mode '%s' error: %s", code, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    # --- away (holiday) ----------------------------------------------------------

    @property
    def is_away_mode_on(self) -> bool | None:
        """True while the device holidays -- the vac PROGRAM or the machMode HOLD of a
        window scheduled by dates; None when neither reading is available."""
        if self._away_code is None:
            return None
        if self._vacation_hold:
            return True
        mode = self._running_mode or self._live_mode
        if mode is None:
            return None
        return mode == self._away_code

    async def async_turn_away_mode_on(self) -> None:
        if self._away_code is None:
            return
        # Remember what to come back to BEFORE switching, so turning away off restores
        # the mode the user actually had rather than a fixed default.
        current = self._live_mode
        if current is not None and current != self._away_code:
            self._coordinator_store(HW_LAST_MODE_STORE)[self._appliance_id] = current
        # Device codes throughout: away never round-trips through the HA names.
        await self._async_apply_mode(self._away_code)

    async def async_turn_away_mode_off(self) -> None:
        # Starting a normal program is the one exit this integration knows. For the vac
        # PROGRAM that is the app's own flow; for a machMode hold (window scheduled by
        # dates) it is a best effort -- the window stays configured on the device, which
        # may re-enter the hold while the dates remain current. Moving/clearing the
        # window is what the date entities are for.
        if not self._normal_codes:
            return
        stored = self._coordinator_store(HW_LAST_MODE_STORE).get(self._appliance_id)
        current = self._live_mode
        if current in self._normal_codes:
            # machMode hold: the program never left its normal setting, so re-starting
            # THAT program is the exit that changes nothing else. (With the vac
            # PROGRAM active, current is the vac code and this branch never matches.)
            target = current
        else:
            target = stored if stored in self._normal_codes else self._normal_codes[0]
        _LOGGER.debug(
            "WaterHeater debug: away off -> '%s' (stored=%r) id=%s",
            target,
            stored,
            redact_id(self._appliance_id),
        )
        await self._async_apply_mode(target)

    # --- shared sender -----------------------------------------------------------

    async def _async_send(self, params: dict, command_name: str, what: str) -> None:
        """Apply `params` to `command_name` and send it, with the usual error wrapping."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            _LOGGER.debug(
                "WaterHeater debug: send %s (cmd=%s) id=%s",
                what,
                command_name,
                redact_id(self._appliance_id),
            )
            await async_send_command(
                self.hass, client, appliance, command_name, params
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("WaterHeater: send error %s: %s", what, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
