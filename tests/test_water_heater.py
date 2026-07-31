# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the water_heater platform (HW heat pump water heaters / WH water heaters).

Modeled on the REAL HP250M7C-F9 schema (full dump): a `startProgram` command carrying
program[auto,eco,elec,vac] + tempSel range[35,75,1], and a `settings` command carrying
onOffStatus range[0,1] plus its OWN tempSel (the one the device silently ignores).

Verifies:
- capability-gating: no writable tempSel -> no entity; the feature bits follow the
  parameters the device actually exposes;
- the setpoint resolves on startProgram FIRST, never on settings (the v5.10 live finding);
- min/max/step read from the REAL parameter at runtime;
- current/target temperature and the operating mode read from the cloud shadow;
- "off" is a synthetic operation backed by onOffStatus, and selecting a program on a
  powered-off device powers it on first;
- away mode maps to the `vac` program and restores the previous mode when switched off.

Stdlib unittest with inline Home Assistant stubs (no HA install required). The stubs are
getattr-guarded so they coexist with the other test modules in the pytest process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_homeassistant_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    device_registry = _mod("homeassistant.helpers.device_registry")
    device_registry.DeviceEntryType = getattr(
        device_registry, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )
    entity_platform = _mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = getattr(entity_platform, "AddEntitiesCallback", object)

    update_coordinator = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    update_coordinator.CoordinatorEntity = getattr(update_coordinator, "CoordinatorEntity", CoordinatorEntity)
    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")
    wh_mod = _mod("homeassistant.components.water_heater")
    wh_mod.WaterHeaterEntity = getattr(wh_mod, "WaterHeaterEntity", type("WaterHeaterEntity", (), {}))
    wh_mod.WaterHeaterEntityFeature = getattr(
        wh_mod,
        "WaterHeaterEntityFeature",
        enum.IntFlag(
            "WaterHeaterEntityFeature",
            {"TARGET_TEMPERATURE": 1, "OPERATION_MODE": 2, "AWAY_MODE": 4, "ON_OFF": 8},
        ),
    )

    # binary_sensor, for the heat-source drift guard below (it imports the HW table).
    import dataclasses

    binary_mod = _mod("homeassistant.components.binary_sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None

    binary_mod.BinarySensorEntityDescription = getattr(
        binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription
    )
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {}))
    binary_mod.BinarySensorDeviceClass = getattr(
        binary_mod,
        "BinarySensorDeviceClass",
        type("BinarySensorDeviceClass", (), {
            "DOOR": "door", "PROBLEM": "problem", "RUNNING": "running",
            "OCCUPANCY": "occupancy", "LIGHT": "light", "CONNECTIVITY": "connectivity",
            "HEAT": "heat",
        }),
    )
    components.binary_sensor = binary_mod

    const = _mod("homeassistant.const")
    const.UnitOfTemperature = getattr(
        const, "UnitOfTemperature", type("UnitOfTemperature", (), {"CELSIUS": "C"})
    )
    const.ATTR_TEMPERATURE = getattr(const, "ATTR_TEMPERATURE", "temperature")
    const.STATE_OFF = getattr(const, "STATE_OFF", "off")
    const.STATE_ON = getattr(const, "STATE_ON", "on")
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    ha.const = const
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    components.water_heater = wh_mod


_install_homeassistant_stubs()


class RangeParam:
    """Mimics HonParameterRange: min/max/step + a value setter that validates."""

    def __init__(self, value, mn, mx, step) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self._v = self._coerce(value)

    @staticmethod
    def _coerce(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return float(str(v).replace(",", "."))

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        fv = self._coerce(v)
        if not (self.min <= fv <= self.max) or ((fv - self.min) * 100) % (self.step * 100):
            raise ValueError(f"Allowed: [{self.min}..{self.max}] step {self.step} But was: {fv}")
        self._v = fv


class ProgramParam:
    """Mimics the startProgram program parameter: a list of category codes."""

    def __init__(self, values, current=None) -> None:
        self._values = [str(v) for v in values]
        self._value = str(current) if current is not None else self._values[0]

    @property
    def values(self):
        return list(self._values)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        if str(v) not in self._values:
            raise ValueError(f"Allowed values: {self._values} But was: {v}")
        self._value = str(v)


class RecordingCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.send_calls = 0
        self.sent = None

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: p.value for k, p in self.parameters.items()}


class FakeAppliance:
    def __init__(self, commands) -> None:
        self.commands = commands


class FakeClient:
    def run_command_sync(self, coro) -> None:
        asyncio.run(coro)


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.refreshes = 0
        self.last_update_success = True
        self.last_exception = None

    async def async_refresh(self) -> None:
        self.refreshes += 1

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _hw_commands(program: str = "eco", *, with_on_off: bool = True) -> dict:
    """HP250M7C-F9-shaped schema: tempSel on BOTH commands, program only on startProgram.

    The settings tempSel deliberately carries a DIFFERENT range so a test can prove which
    of the two the entity resolved.
    """
    settings_params = {"tempSel": RangeParam(60, 50, 70, 5)}
    if with_on_off:
        settings_params["onOffStatus"] = RangeParam(1, 0, 1, 1)
    return {
        "startProgram": RecordingCommand(
            {
                "program": ProgramParam(["auto", "eco", "elec", "vac"], program),
                "tempSel": RangeParam(55, 35, 75, 1),
            }
        ),
        "settings": RecordingCommand(settings_params),
    }


def _attrs(**overrides) -> dict:
    base = {
        "temp": "48",
        "tempSel": "55",
        "onOffStatus": "1",
        "startProgram.program": "eco",
    }
    base.update(overrides)
    return base


async def _build(app_type: str, appliance, attributes: dict, client=None) -> list:
    from custom_components.addhon import water_heater
    from custom_components.addhon.const import DOMAIN

    data = {
        "x-1": {
            "type": app_type,
            "name": "Boiler",
            "attributes": attributes,
            "appliance": appliance,
        }
    }
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}})
    added: list = []
    await water_heater.async_setup_entry(hass, FakeEntry(), added.extend)
    for ent in added:
        ent.hass = hass
    return added


def _one(app_type: str = "HW", commands=None, attributes=None, client=None):
    commands = _hw_commands() if commands is None else commands
    appliance = FakeAppliance(commands)
    added = asyncio.run(
        _build(app_type, appliance, _attrs() if attributes is None else attributes, client=client)
    )
    return added, commands


class GatingTest(unittest.TestCase):
    def test_entity_created_for_hw(self) -> None:
        added, _ = _one()
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]._attr_unique_id, "x-1_water_heater")

    def test_no_entity_without_writable_setpoint(self) -> None:
        commands = {"settings": RecordingCommand({"onOffStatus": RangeParam(1, 0, 1, 1)})}
        added, _ = _one(commands=commands)
        self.assertEqual(added, [])

    def test_other_appliance_types_ignored(self) -> None:
        # An AC also exposes tempSel on settings; it must NOT get a water_heater entity.
        added, _ = _one(app_type="AC")
        self.assertEqual(added, [])

    def test_plain_water_heater_type_supported_when_it_has_a_setpoint(self) -> None:
        added, _ = _one(app_type="WH")
        self.assertEqual(len(added), 1)

    def test_features_follow_the_schema(self) -> None:
        from homeassistant.components.water_heater import WaterHeaterEntityFeature

        added, _ = _one()
        features = added[0]._attr_supported_features
        self.assertTrue(features & WaterHeaterEntityFeature.TARGET_TEMPERATURE)
        self.assertTrue(features & WaterHeaterEntityFeature.OPERATION_MODE)
        self.assertTrue(features & WaterHeaterEntityFeature.ON_OFF)
        self.assertTrue(features & WaterHeaterEntityFeature.AWAY_MODE)

    def test_no_on_off_feature_without_the_parameter(self) -> None:
        from homeassistant.components.water_heater import WaterHeaterEntityFeature

        added, _ = _one(commands=_hw_commands(with_on_off=False))
        entity = added[0]
        self.assertFalse(entity._attr_supported_features & WaterHeaterEntityFeature.ON_OFF)
        # ...and then "off" is not offered as an operation either.
        self.assertNotIn("off", entity._attr_operation_list)


class ReadStateTest(unittest.TestCase):
    def test_setpoint_resolves_on_start_program_not_settings(self) -> None:
        # Load-bearing: a tempSel sent through `settings` is silently ignored by the
        # device, so both the range and the write path must come from startProgram.
        added, _ = _one()
        entity = added[0]
        self.assertEqual(entity._temp_command, "startProgram")
        self.assertEqual(
            (entity.min_temp, entity.max_temp, entity.target_temperature_step),
            (35.0, 75.0, 1.0),
        )

    def test_range_follows_the_start_program_swap(self) -> None:
        # Selecting a program REPLACES appliance.commands["startProgram"] with the chosen
        # category's own command; the bounds must follow it, not the init-time snapshot.
        added, commands = _one()
        entity = added[0]
        self.assertEqual((entity.min_temp, entity.max_temp), (35.0, 75.0))
        commands["startProgram"] = RecordingCommand(
            {
                "program": ProgramParam(["auto", "eco", "elec", "vac"], "vac"),
                "tempSel": RangeParam(40, 40, 60, 1),
            }
        )
        self.assertEqual((entity.min_temp, entity.max_temp), (40.0, 60.0))

    def test_temperatures_from_shadow(self) -> None:
        added, _ = _one()
        entity = added[0]
        self.assertEqual(entity.current_temperature, 48.0)
        self.assertEqual(entity.target_temperature, 55.0)

    def test_non_numeric_temperature_is_unknown(self) -> None:
        added, _ = _one(attributes=_attrs(temp="--"))
        self.assertIsNone(added[0].current_temperature)

    def test_operation_list_uses_ha_standard_names(self) -> None:
        # The frontend's mode picker resolves its icons from a HARDCODED map of the
        # standard states, so the device codes are reported under those names.
        added, _ = _one()
        self.assertEqual(
            added[0]._attr_operation_list,
            ["off", "heat_pump", "eco", "electric", "vac"],
        )

    def test_current_operation_from_shadow(self) -> None:
        added, _ = _one()
        self.assertEqual(added[0].current_operation, "eco")

    def test_current_operation_is_mapped_to_the_standard_name(self) -> None:
        added, _ = _one(attributes=_attrs(**{"startProgram.program": "auto"}))
        self.assertEqual(added[0].current_operation, "heat_pump")

    def test_ambiguous_mapping_falls_back_to_raw_device_codes(self) -> None:
        # Two device codes claiming the same standard state must never collapse: the
        # round-trip would then start the wrong program.
        commands = _hw_commands()
        commands["startProgram"] = RecordingCommand(
            {
                "program": ProgramParam(["elec", "electric", "eco"], "eco"),
                "tempSel": RangeParam(55, 35, 75, 1),
            }
        )
        added, _ = _one(commands=commands)
        self.assertEqual(
            added[0]._attr_operation_list, ["off", "elec", "electric", "eco"]
        )

    def test_current_operation_off_wins_over_program(self) -> None:
        added, _ = _one(attributes=_attrs(onOffStatus="0"))
        self.assertEqual(added[0].current_operation, "off")

    def test_unknown_program_reports_unknown(self) -> None:
        added, _ = _one(attributes=_attrs(**{"startProgram.program": "quantum"}))
        self.assertIsNone(added[0].current_operation)

    def test_program_falls_back_to_the_command_value(self) -> None:
        # No shadow mirror: the category recovered at command load is the active mode.
        attributes = _attrs()
        attributes.pop("startProgram.program")
        added, _ = _one(commands=_hw_commands("elec"), attributes=attributes)
        self.assertEqual(added[0].current_operation, "electric")

    def test_action_and_source_when_the_compressor_runs(self) -> None:
        added, _ = _one(attributes=_attrs(
            compressorHeatingCurrentStatus="1",
            electricHeatingCurrentStatus="0",
        ))
        self.assertEqual(
            added[0].extra_state_attributes,
            {"action": "heating", "heat_source": "compressor"},
        )

    def test_action_is_idle_when_on_but_no_source_runs(self) -> None:
        added, _ = _one(attributes=_attrs(
            compressorHeatingCurrentStatus="0",
            electricHeatingCurrentStatus="0",
        ))
        self.assertEqual(
            added[0].extra_state_attributes,
            {"action": "idle", "heat_source": "none"},
        )

    def test_action_is_off_when_the_device_is_powered_off(self) -> None:
        added, _ = _one(attributes=_attrs(
            onOffStatus="0",
            compressorHeatingCurrentStatus="0",
        ))
        self.assertEqual(added[0].extra_state_attributes["action"], "off")

    def test_several_sources_collapse_to_multiple(self) -> None:
        # A scalar, not a list: HA only translates scalar attribute values, and the
        # per-source detail lives in the binary sensors.
        added, _ = _one(attributes=_attrs(
            compressorHeatingCurrentStatus="1",
            electricHeatingCurrentStatus="1",
        ))
        self.assertEqual(added[0].extra_state_attributes["heat_source"], "multiple")

    def test_hot_water_level_is_the_calibrated_percentage(self) -> None:
        # remainingWaterLevel is a 0..12 gauge, not a percentage: 12 == full.
        added, _ = _one(attributes=_attrs(remainingWaterLevel="12"))
        self.assertEqual(added[0].extra_state_attributes["hot_water_level"], 100.0)
        added, _ = _one(attributes=_attrs(remainingWaterLevel="6"))
        self.assertEqual(added[0].extra_state_attributes["hot_water_level"], 50.0)

    def test_hot_water_level_shares_the_sensor_calibration(self) -> None:
        # One helper, so the attribute and the hot_water_level sensor cannot drift.
        from custom_components.addhon.hw_values import hw_water_level

        added, _ = _one(attributes=_attrs(remainingWaterLevel="10"))
        self.assertEqual(
            added[0].extra_state_attributes["hot_water_level"], hw_water_level("10")
        )

    def test_hot_water_level_alone_still_produces_attributes(self) -> None:
        # Independently gated: no heat-source telemetry must not suppress the level.
        added, _ = _one(attributes=_attrs(remainingWaterLevel="3"))
        self.assertEqual(added[0].extra_state_attributes, {"hot_water_level": 25.0})

    def test_non_numeric_water_level_is_omitted(self) -> None:
        added, _ = _one(attributes=_attrs(remainingWaterLevel="--"))
        self.assertIsNone(added[0].extra_state_attributes)

    def test_no_attributes_when_the_device_reports_no_source(self) -> None:
        # A confident "not heating" on a device that never reports it would be a lie.
        added, _ = _one()
        self.assertIsNone(added[0].extra_state_attributes)

    def test_away_mode_reads_the_vac_program(self) -> None:
        added, _ = _one(attributes=_attrs(**{"startProgram.program": "vac"}))
        self.assertIs(added[0].is_away_mode_on, True)
        added, _ = _one()
        self.assertIs(added[0].is_away_mode_on, False)


class HeatSourceDriftTest(unittest.TestCase):
    """The heat-source attribute names must stay identical to the ones the HW binary
    sensors read. Those are reality-checked against the real HP250M7C-F9 dump; a rename
    on that side would otherwise leave `heating` silently stuck reporting nothing."""

    def test_names_come_from_the_hw_binary_sensor_table(self) -> None:
        from custom_components.addhon import binary_sensor, water_heater

        known = {desc.attr_key for desc in binary_sensor._HEAT_PUMP_BINARY}
        used = {attr for _name, attr in water_heater.HW_HEAT_SOURCES}
        self.assertEqual(
            used - known,
            set(),
            "heat-source attributes absent from _HEAT_PUMP_BINARY (unverified against "
            "the real device schema)",
        )

    def test_protection_statuses_are_not_treated_as_heating(self) -> None:
        from custom_components.addhon import water_heater

        used = {attr for _name, attr in water_heater.HW_HEAT_SOURCES}
        self.assertNotIn("antifreezingStatus", used)
        self.assertNotIn("autoDefrostStatus", used)


class WriteTest(unittest.TestCase):
    def test_set_temperature_sends_start_program(self) -> None:
        from homeassistant.const import ATTR_TEMPERATURE

        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 62.0}))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # Sent as a clean int (62, not 62.0) and NOT through the settings command.
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 62)
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_set_operation_mode_sends_the_program(self) -> None:
        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("electric"))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # The HA standard name is translated BACK to the device's own code.
        self.assertEqual(commands["startProgram"].parameters["program"].value, "elec")
        # Device already on: no power command needed.
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_set_operation_mode_powers_on_first_when_off(self) -> None:
        added, commands = _one(attributes=_attrs(onOffStatus="0"), client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("heat_pump"))
        self.assertEqual(commands["settings"].send_calls, 1)
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 1)
        self.assertEqual(commands["startProgram"].parameters["program"].value, "auto")

    def test_set_operation_mode_off_writes_power(self) -> None:
        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("off"))
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 0)
        self.assertEqual(commands["startProgram"].send_calls, 0)

    def test_unknown_operation_mode_raises(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        added, commands = _one(client=FakeClient())
        with self.assertRaises(HomeAssistantError):
            asyncio.run(added[0].async_set_operation_mode("turbo"))
        self.assertEqual(commands["startProgram"].send_calls, 0)

    def test_turn_on_and_off(self) -> None:
        added, commands = _one(client=FakeClient())
        entity = added[0]
        asyncio.run(entity.async_turn_off())
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 0)
        asyncio.run(entity.async_turn_on())
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 1)


class AwayModeTest(unittest.TestCase):
    def test_turn_away_on_selects_vac(self) -> None:
        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_turn_away_mode_on())
        self.assertEqual(commands["startProgram"].parameters["program"].value, "vac")

    def test_turn_away_off_restores_the_previous_mode(self) -> None:
        added, commands = _one(attributes=_attrs(**{"startProgram.program": "elec"}),
                               client=FakeClient())
        entity = added[0]
        asyncio.run(entity.async_turn_away_mode_on())
        self.assertEqual(commands["startProgram"].parameters["program"].value, "vac")
        asyncio.run(entity.async_turn_away_mode_off())
        self.assertEqual(commands["startProgram"].parameters["program"].value, "elec")

    def test_turn_away_off_without_memory_picks_the_first_normal_mode(self) -> None:
        added, commands = _one(attributes=_attrs(**{"startProgram.program": "vac"}),
                               client=FakeClient())
        asyncio.run(added[0].async_turn_away_mode_off())
        self.assertEqual(commands["startProgram"].parameters["program"].value, "auto")


if __name__ == "__main__":
    unittest.main()
