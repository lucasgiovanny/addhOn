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
- on/off is a plain 0/1 parameter of the settings command.

COEXISTENCE by design: the HW `number.target_temp`, `select.hw_mode` and
`switch.on_off` entities stay. They read and write the SAME parameters through the same
senders, so they cannot drift from this entity (both go through the coordinator), and
they remain the convenient handle for templates/automations plus the only surface for
the controls the water_heater domain has no slot for (boost, silent, child lock,
sterilization, per-mode setpoints).
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
from .hon_commands import async_send_command, find_settings_param, param_range
from .program_options import (
    async_send_program,
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

# Power parameter, resolved on the usual settings/setParameters pair.
HW_ON_OFF_PARAM = "onOffStatus"

# Current water temperature in the cloud shadow (same attribute the `water_temp` sensor
# reads).
HW_CURRENT_TEMP_ATTR = "temp"

# The heat sources the appliance reports as currently running, ground-truthed on the real
# HP250M7C-F9 (the same attributes the compressor / electric-heater binary sensors read).
# Mapped to stable machine names so the exposed attribute is language-neutral and
# templatable. Protection statuses (antifreeze, defrost) are deliberately NOT here: they
# are not heating the water.
HW_HEAT_SOURCES: tuple[tuple[str, str], ...] = (
    ("compressor", "compressorHeatingCurrentStatus"),
    ("electric_heater", "electricHeatingCurrentStatus"),
    ("aux_electric_heater", "auxElecHeatingStatus"),
    ("boiler", "boilerHeatingCurrentStatus"),
)

# The holiday/vacation program. On the device it is an ordinary startProgram mode, but
# HA models "away" as its own orthogonal toggle, so it is exposed BOTH ways: as an
# operation-mode option and as the away switch. Both read and write the same program, so
# they can never disagree.
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
        on_off = find_settings_param(appliance, HW_ON_OFF_PARAM)
        self._on_off_command = on_off[0] if on_off is not None else None

        # --- operating modes ------------------------------------------------------
        program = startprogram_program_param(appliance)
        self._program_param_name = program[1] if program is not None else None
        self._mode_codes = startprogram_program_codes(appliance)
        # The device's own spelling of the holiday program (never assume lowercase).
        self._away_code = next(
            (code for code in self._mode_codes if code.lower() == HW_AWAY_CODE), None
        )
        self._normal_codes = [code for code in self._mode_codes if code != self._away_code]

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
            options = ([STATE_OFF] if has_power else []) + self._mode_codes
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
    def _live_temp_range(self) -> tuple[float, float, float]:
        """(min, max, step) read from the CURRENT runtime parameter.

        Re-resolved on every access rather than read off the __init__ snapshot: selecting
        a program REPLACES appliance.commands["startProgram"] with the chosen category's
        own command object (see async_send_program), so the snapshot would keep reporting
        the pre-swap category's bounds. Falls back to the snapshot, then to the static
        range, so a device that errored on load still offers a sane control.
        """
        param = None
        resolved = find_settings_param(self._appliance, HW_TEMP_PARAM, HW_TEMP_COMMANDS)
        if resolved is not None:
            param = resolved[1]
        elif self._temp_param is not None:
            param = self._temp_param
        if param is None:
            return self._temp_fallback_range
        return param_range(param) or self._temp_fallback_range

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
        """`heating` plus the heat sources currently running.

        The water_heater domain has NO equivalent of climate's hvac_action: the entity
        state is the SELECTED MODE, not what the appliance is doing. So this is exposed
        as attributes rather than folded into current_operation -- a synthetic "heating"
        state would overwrite the mode AND put a value in the state machine that is not a
        member of operation_list, leaving HA's mode picker with nothing selected.

        Capability-gated per source: a status the device does not report is omitted
        instead of being reported False, and a device that reports none of them gets no
        `heating` key at all rather than a confident "not heating". Attribute changes are
        recorded, so `heating` gets real history even though it is not its own entity --
        the per-source binary sensors remain the graphable/automatable handles.
        """
        running: list[str] = []
        reported = False
        for name, attr in HW_HEAT_SOURCES:
            raw = self._get_attr(attr)
            if raw is None:
                continue
            reported = True
            if str(raw).strip() == "1":
                running.append(name)
        if not reported:
            return None
        return {"heating": bool(running), "heat_sources": running}

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
        # ALWAYS send a string, and a clean one: the client's str_to_float does
        # int(string) and catches only ValueError, so a fractional float (55.5) would be
        # truncated to 55 WITHOUT error, while "55.5" stays 55.5 and the range setter
        # validates the step. An integer serializes as "55", never "55.0" (an enum/range
        # setter rejects the latter).
        number = float(value)
        send_value = str(int(number)) if number.is_integer() else str(number)
        await self._async_send(
            {HW_TEMP_PARAM: send_value},
            self._temp_command,
            f"{HW_TEMP_PARAM}={send_value}",
        )
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

    async def _async_write_power(self, value: str) -> None:
        """Send onOffStatus WITHOUT refreshing (the callers batch the refresh)."""
        if self._on_off_command is None:
            return
        await self._async_send(
            {HW_ON_OFF_PARAM: value},
            self._on_off_command,
            f"{HW_ON_OFF_PARAM}={value}",
        )

    # --- operating mode ----------------------------------------------------------

    @property
    def _live_mode(self) -> str | None:
        """The active program, matched against the codes the device offers.

        Reads the poll-refreshed cloud shadow first and falls back to the value the
        command carries (recovered at command load). Double-gated against the offered
        codes: an unrecognized reading reports unknown rather than a guessed mode. Same
        contract as the HW mode select.
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
    def current_operation(self) -> str | None:
        # Power wins: a device that is off is off, whichever program it would resume.
        if self._on_off_command is not None and self._is_off:
            return STATE_OFF
        if self._mode_codes:
            return self._live_mode
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
        # Selecting a program on a powered-off device would leave it off (startProgram
        # does not imply power on), so power it back up first -- one extra command, only
        # when the device actually reports itself off.
        if self._on_off_command is not None and self._is_off:
            _LOGGER.debug(
                "WaterHeater debug: powering on before program '%s' id=%s",
                operation_mode,
                redact_id(self._appliance_id),
            )
            await self._async_write_power("1")
        await self._async_send_mode(operation_mode)
        await self._async_request_command_refresh()

    async def _async_send_mode(self, code: str) -> None:
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
            await async_send_program(self.hass, client, appliance, code)
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
        """True while the holiday program is active; None when the mode is unknown."""
        if self._away_code is None:
            return None
        mode = self._live_mode
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
        await self.async_set_operation_mode(self._away_code)

    async def async_turn_away_mode_off(self) -> None:
        if not self._normal_codes:
            return
        stored = self._coordinator_store(HW_LAST_MODE_STORE).get(self._appliance_id)
        target = stored if stored in self._normal_codes else self._normal_codes[0]
        _LOGGER.debug(
            "WaterHeater debug: away off -> '%s' (stored=%r) id=%s",
            target,
            stored,
            redact_id(self._appliance_id),
        )
        await self.async_set_operation_mode(target)

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
