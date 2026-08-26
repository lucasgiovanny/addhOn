# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the water_heater platform (HW heat pump water heaters / WH water heaters).

Modeled on the REAL HP250M7C-F9 schema (full dump): a `startProgram` command carrying
machMode + program[auto,eco,elec,vac] + onOffStatus + tempSel range[35,75,1], and a
`settings` command carrying onOffStatus range[0,1], its OWN tempSel (the one the device
silently ignores) and the mandatory `operationName` fixed to "grSetVacDate" that pins the
whole command to one operation.

Verifies:
- capability-gating: no writable tempSel -> no entity; the feature bits follow the
  parameters the device actually exposes;
- the setpoint resolves on startProgram FIRST, never on settings (the v5.10 live finding);
- min/max/step read from the REAL parameter at runtime;
- current/target temperature and the operating mode read from the cloud shadow;
- "off" is a synthetic operation backed by onOffStatus, resolved on startProgram (the
  settings command is pinned to grSetVacDate and would swallow it), and selecting a
  program on a powered-off device powers it on IN THE SAME send;
- a device whose only onOffStatus sits on a pinned settings command gets NO power
  capability at all, rather than an "off" that does nothing;
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

    def __init__(self, value, mn, mx, step, mandatory: int = 0) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self.mandatory = mandatory
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


class FixedParam:
    """Mimics HonParameterFixed: a single declared value, and a setter that does NOT
    validate (the engine relies on that -- it is how the vacation dates, and now the
    power flag, are written through parameters the schema calls "fixed")."""

    def __init__(self, value, mandatory: int = 0) -> None:
        self.mandatory = mandatory
        self._value = str(value)

    @property
    def values(self):
        return [self._value]

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = str(v)


class RecordingCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.send_calls = 0
        self.sent = None
        # Mirrors HonCommand.categories for a command with no per-program split.
        self.categories = {}

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


def _hw_commands(
    program: str = "eco",
    *,
    with_on_off: bool = True,
    power_on_start_program: bool = True,
    pinned_operation: str | None = "grSetVacDate",
    with_categories: bool = True,
) -> dict:
    """HP250M7C-F9-shaped schema, as the 2026-08 dumps report it.

    tempSel AND onOffStatus exist on BOTH commands; program only on startProgram. The
    settings tempSel deliberately carries a DIFFERENT range so a test can prove which of
    the two the entity resolved.

    `pinned_operation` reproduces the load-bearing detail: `operationName` is a MANDATORY
    fixed parameter offering exactly one value, so every settings write reaches the
    appliance labelled as that operation and everything outside it is dropped. Set it to
    None for an appliance whose settings command is a free write.

    `power_on_start_program` / `with_on_off` control where onOffStatus exists at all.
    """
    settings_params = {"tempSel": RangeParam(60, 50, 70, 5)}
    if with_on_off:
        settings_params["onOffStatus"] = RangeParam(1, 0, 1, 1)
    if pinned_operation is not None:
        # mandatory=1 is what makes the window writable through this command, and
        # onOffStatus (mandatory 0, above) NOT writable -- the real schema, and the
        # distinction the whole gate rests on.
        settings_params["operationName"] = FixedParam(pinned_operation, mandatory=1)
        settings_params["vacStartDate"] = FixedParam("2000-01-01", mandatory=1)
        settings_params["vacEndDate"] = FixedParam("2000-01-01", mandatory=1)
    codes = ["auto", "eco", "elec", "vac"]
    start_params = {
        "machMode": FixedParam("1"),
        "program": ProgramParam(codes, program),
        "tempSel": RangeParam(55, 35, 75, 1),
    }
    if with_on_off and power_on_start_program:
        start_params["onOffStatus"] = FixedParam("1")
    start = RecordingCommand(start_params)
    if with_categories:
        # One category per program, each pinning its OWN machMode -- the real schema
        # (auto 1, eco 2, elec 3, vac 4). This is what makes machMode readable as "which
        # program is running"; a fixture without it exercises the fallback instead.
        siblings = {}
        for index, code in enumerate(codes, start=1):
            sibling = (
                start
                if code == program
                else RecordingCommand(
                    {
                        "machMode": FixedParam(str(index)),
                        "program": ProgramParam(codes, code),
                        "tempSel": RangeParam(55, 35, 75, 1),
                    }
                )
            )
            sibling.parameters["machMode"] = FixedParam(str(index))
            sibling.categories = siblings
            siblings[code] = sibling
        start.categories = siblings
    return {
        "startProgram": start,
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
                "onOffStatus": FixedParam("1"),
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
    on that side would otherwise leave `heating` silently stuck reporting nothing.

    The table lives in hw_values (HA-free), which is what lets the water_heater
    attributes and the heating_status / heat_source sensors share one derivation.
    """

    def test_names_come_from_the_hw_binary_sensor_table(self) -> None:
        from custom_components.addhon import binary_sensor
        from custom_components.addhon.hw_values import HW_HEAT_SOURCES

        known = {desc.attr_key for desc in binary_sensor._HEAT_PUMP_BINARY}
        used = {attr for _name, attr in HW_HEAT_SOURCES}
        self.assertEqual(
            used - known,
            set(),
            "heat-source attributes absent from _HEAT_PUMP_BINARY (unverified against "
            "the real device schema)",
        )

    def test_protection_statuses_are_not_treated_as_heating(self) -> None:
        from custom_components.addhon.hw_values import HW_HEAT_SOURCES

        used = {attr for _name, attr in HW_HEAT_SOURCES}
        self.assertNotIn("antifreezingStatus", used)
        self.assertNotIn("autoDefrostStatus", used)

    def test_the_entity_and_the_sensors_read_the_same_power_flag(self) -> None:
        # water_heater resolves onOffStatus as a writable COMMAND parameter while the
        # sensors only ever read the shadow; both must name the same key or a powered-off
        # appliance would read `off` on one surface and `idle` on the other.
        from custom_components.addhon import water_heater
        from custom_components.addhon.hw_values import HW_POWER_ATTR

        self.assertEqual(water_heater.HW_ON_OFF_PARAM, HW_POWER_ATTR)


class WriteTest(unittest.TestCase):
    def test_set_temperature_sends_start_program(self) -> None:
        from homeassistant.const import ATTR_TEMPERATURE

        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 62.0}))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # Sent as a clean int (62, not 62.0) and NOT through the settings command.
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 62)
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_off_grid_setpoint_is_snapped_to_the_device_grid(self) -> None:
        # The bug this guards: HA does not enforce the step. Its dial seeds itself from
        # the entity state and adds the step to it, so a shadow reporting an off-grid
        # setpoint (live HP250M7C-F9: tempSel 59.2 on range[35,75,1]) made every press
        # send 60.2 / 61.2, which the Range setter refuses -- the setpoint never moved.
        from homeassistant.const import ATTR_TEMPERATURE

        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 60.2}))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # Nearest grid point, as a clean int (never "60.2", never "60.0").
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 60)
        self.assertEqual(commands["startProgram"].sent["tempSel"], 60)

    def test_setpoint_snaps_up_when_nearer_the_next_grid_point(self) -> None:
        from homeassistant.const import ATTR_TEMPERATURE

        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 60.8}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 61)

    def test_setpoint_snaps_onto_a_fractional_step(self) -> None:
        # A half-degree grid must keep the half degree (no int() rounding).
        from homeassistant.const import ATTR_TEMPERATURE

        commands = _hw_commands()
        commands["startProgram"].parameters["tempSel"] = RangeParam(55, 35, 75, 0.5)
        added, commands = _one(commands=commands, client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 60.4}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 60.5)

    def test_setpoint_is_clamped_to_the_device_bounds(self) -> None:
        from homeassistant.const import ATTR_TEMPERATURE

        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 120.0}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 75)
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 10.0}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 35)

    def test_setpoint_is_not_snapped_without_a_device_range(self) -> None:
        # tempSel with NO min/max/step: there is no declared grid, so the value goes out
        # untouched (snapping onto the UI fallback could drop a value the device accepts).
        from homeassistant.const import ATTR_TEMPERATURE

        class PlainParam:
            def __init__(self, value) -> None:
                self.value = value

        commands = _hw_commands()
        commands["startProgram"].parameters["tempSel"] = PlainParam(55)
        added, commands = _one(commands=commands, client=FakeClient())
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 60.2}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, "60.2")

    def test_set_operation_mode_sends_the_program(self) -> None:
        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("electric"))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # The HA standard name is translated BACK to the device's own code.
        self.assertEqual(commands["startProgram"].parameters["program"].value, "elec")
        # Device already on: no power command needed.
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_set_operation_mode_powers_on_in_the_same_send_when_off(self) -> None:
        # Power lives on startProgram, so powering on before a program change costs no
        # second command: one send carrying BOTH, which is also the envelope the
        # official app uses (its command history: machMode + onOffStatus + tempSel).
        added, commands = _one(attributes=_attrs(onOffStatus="0"), client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("heat_pump"))
        start = commands["startProgram"]
        self.assertEqual(start.send_calls, 1)
        self.assertEqual(start.parameters["program"].value, "auto")
        self.assertEqual(start.parameters["onOffStatus"].value, "1")
        # The pinned settings command is never touched.
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_set_operation_mode_off_writes_power_on_start_program(self) -> None:
        added, commands = _one(client=FakeClient())
        asyncio.run(added[0].async_set_operation_mode("off"))
        self.assertEqual(commands["startProgram"].parameters["onOffStatus"].value, "0")
        self.assertEqual(commands["startProgram"].send_calls, 1)
        # NOT through settings: that command is pinned to grSetVacDate, which is exactly
        # why "off" did nothing before v5.22.0.
        self.assertEqual(commands["settings"].send_calls, 0)
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 1)

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
        self.assertEqual(commands["startProgram"].parameters["onOffStatus"].value, "0")
        asyncio.run(entity.async_turn_on())
        self.assertEqual(commands["startProgram"].parameters["onOffStatus"].value, "1")


class StartProgramEnvelopeTest(unittest.TestCase):
    """Every startProgram write must name the program the appliance is RUNNING.

    The program rides on the command CATEGORY, not on a payload parameter:
    api.send_command derives `programName` from the active category's own name, and the
    real appliance's accepted commands carry only {machMode, onOffStatus, tempSel}. After
    a restart the active category is the schema's FIRST one, so a setpoint or power write
    sent on it would have told a device running `eco` to start `auto`.
    """

    def _entity(self, commands, running: str):
        # Command sitting on "auto" (the schema's first), appliance actually on `running`.
        added = asyncio.run(
            _build(
                "HW",
                FakeAppliance(commands),
                _attrs(**{"startProgram.program": running}),
                client=FakeClient(),
            )
        )
        return added[0]

    def test_a_setpoint_write_re_asserts_the_running_program(self) -> None:
        from homeassistant.const import ATTR_TEMPERATURE

        commands = _hw_commands("auto")
        entity = self._entity(commands, "eco")
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 58.0}))
        start = commands["startProgram"]
        self.assertEqual(start.parameters["tempSel"].value, 58)
        self.assertEqual(start.parameters["program"].value, "eco")

    def test_a_power_write_re_asserts_the_running_program(self) -> None:
        commands = _hw_commands("auto")
        entity = self._entity(commands, "elec")
        asyncio.run(entity.async_turn_off())
        start = commands["startProgram"]
        self.assertEqual(start.parameters["onOffStatus"].value, "0")
        self.assertEqual(start.parameters["program"].value, "elec")

    def test_an_unknown_running_program_falls_back_to_a_plain_send(self) -> None:
        # Never a guessed program: with nothing to re-assert the write goes out on the
        # active category, exactly as before.
        from homeassistant.const import ATTR_TEMPERATURE

        commands = _hw_commands("auto")
        attributes = _attrs()
        attributes.pop("startProgram.program")
        commands["startProgram"].parameters["program"] = ProgramParam(
            ["auto", "eco", "elec", "vac"], "auto"
        )
        added = asyncio.run(
            _build("HW", FakeAppliance(commands), attributes, client=FakeClient())
        )
        asyncio.run(added[0].async_set_temperature(**{ATTR_TEMPERATURE: 58.0}))
        start = commands["startProgram"]
        self.assertEqual(start.parameters["tempSel"].value, 58)
        self.assertEqual(start.parameters["program"].value, "auto")

    def test_a_settings_setpoint_is_not_routed_through_start_program(self) -> None:
        # A model whose setpoint lives on an unpinned settings command keeps the plain
        # path: there is no program envelope to worry about there.
        from homeassistant.const import ATTR_TEMPERATURE

        commands = _hw_commands(pinned_operation=None)
        del commands["startProgram"].parameters["tempSel"]
        added = asyncio.run(
            _build("HW", FakeAppliance(commands), _attrs(), client=FakeClient())
        )
        entity = added[0]
        self.assertEqual(entity._temp_command, "settings")
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 60.0}))
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 60)
        self.assertEqual(commands["startProgram"].send_calls, 0)


class PowerCommandResolutionTest(unittest.TestCase):
    """Where the power flag is written -- the v5.22.0 fix.

    The HP250M7C-F9 exposes onOffStatus on BOTH commands, and only one of them works:
    `settings` declares `operationName` as a MANDATORY fixed parameter offering the single
    value "grSetVacDate" (identical on four dumps between 2026-07-26 and 2026-08-25), and
    `command.send()` transmits the whole parameter group, so the appliance reads every
    settings write as "set the vacation dates" and drops the rest. The dumps show both
    halves of that: the vacation window written from Home Assistant landed, while
    onOffStatus stayed 1 through every capture with the entity reporting "off" sent.
    """

    def test_power_resolves_on_start_program(self) -> None:
        added, _ = _one()
        self.assertEqual(added[0]._on_off_command, "startProgram")

    def test_power_falls_back_to_settings_when_start_program_has_none(self) -> None:
        # Another model may keep power only on a settings command that is NOT pinned --
        # that must keep working exactly as before.
        commands = _hw_commands(power_on_start_program=False, pinned_operation=None)
        added, _ = _one(commands=commands, client=FakeClient())
        entity = added[0]
        self.assertEqual(entity._on_off_command, "settings")
        asyncio.run(entity.async_turn_off())
        self.assertEqual(commands["settings"].parameters["onOffStatus"].value, 0)

    def test_pinned_settings_operation_removes_the_power_capability(self) -> None:
        # onOffStatus exists, but ONLY on a command pinned to another operation: the
        # appliance would accept the payload and ignore it, so the capability is dropped
        # rather than offered as an "off" that does nothing.
        from homeassistant.components.water_heater import WaterHeaterEntityFeature

        commands = _hw_commands(power_on_start_program=False)
        added, _ = _one(commands=commands)
        entity = added[0]
        self.assertIsNone(entity._on_off_command)
        self.assertFalse(
            entity._attr_supported_features & WaterHeaterEntityFeature.ON_OFF
        )
        self.assertNotIn("off", entity._attr_operation_list)

    def test_a_free_settings_command_keeps_the_capability(self) -> None:
        # Only a PINNED command gates anything: an appliance whose settings command is an
        # ordinary multi-parameter write must never lose a control to this rule.
        commands = _hw_commands(power_on_start_program=False, pinned_operation=None)
        added, _ = _one(commands=commands)
        self.assertEqual(added[0]._on_off_command, "settings")

    def test_a_mandatory_flag_does_not_rescue_power_on_a_pinned_command(self) -> None:
        # v5.26 keyed the gate on the mandatory flag; the opp2 experiment killed that
        # (a mandatory field echoed and was discarded). Since v5.30 the gate keys on
        # the known-operation table, and no operation is known for onOffStatus -- so a
        # pinned command loses the capability whatever the flag says.
        commands = _hw_commands(power_on_start_program=False)
        commands["settings"].parameters["onOffStatus"] = RangeParam(
            1, 0, 1, 1, mandatory=1
        )
        added, _ = _one(commands=commands)
        self.assertIsNone(added[0]._on_off_command)


class RealDeviceSchemaTest(unittest.TestCase):
    """The whole entity, built from the REAL HP250M7C-F9 command schema.

    The other tests use a hand-written fixture; this one rebuilds the commands straight
    from tests/fixtures/hw_hp250/device_schema.json (the redacted 2026-08-25 dump), so a
    schema detail the hand-written one gets wrong cannot hide the regression.
    """

    @staticmethod
    def _commands() -> dict:
        import json
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "tests" / "fixtures" / "hw_hp250" / "device_schema.json"
        )
        fixture = json.loads(path.read_text(encoding="utf-8"))

        def build(params):
            built = {}
            for p_name, meta in params.items():
                typology = meta.get("typology")
                if typology == "range":
                    built[p_name] = RangeParam(
                        meta["min"], meta["min"], meta["max"], meta["step"]
                    )
                elif typology == "enum" and len(meta.get("enum", [])) > 1:
                    # The SELECTED member, not the first: each startProgram category
                    # names its own program that way.
                    built[p_name] = ProgramParam(
                        meta["enum"], meta.get("value", meta["enum"][0])
                    )
                else:
                    built[p_name] = FixedParam(
                        meta.get("value", meta.get("enum", ["0"])[0])
                    )
            return RecordingCommand(built)

        commands = {name: build(params) for name, params in fixture["commands"].items()}
        # The per-program categories, which carry the machMode pairing.
        for cmd_name, categories in fixture.get("command_categories", {}).items():
            siblings = {
                cat: build(params) for cat, params in categories.items()
            }
            for sibling in siblings.values():
                sibling.categories = siblings
            active = commands.get(cmd_name)
            if active is not None:
                active.categories = siblings
        return commands

    def _entity(self, commands, attributes=None):
        added = asyncio.run(
            _build(
                "HW",
                FakeAppliance(commands),
                _attrs() if attributes is None else attributes,
                client=FakeClient(),
            )
        )
        self.assertEqual(len(added), 1)
        return added[0]

    def test_the_setpoint_and_the_power_both_resolve_on_start_program(self) -> None:
        entity = self._entity(self._commands())
        self.assertEqual(entity._temp_command, "startProgram")
        self.assertEqual(entity._on_off_command, "startProgram")

    def test_the_real_schema_still_offers_off(self) -> None:
        entity = self._entity(self._commands())
        self.assertEqual(
            entity._attr_operation_list, ["off", "heat_pump", "eco", "electric", "vac"]
        )

    def test_off_reaches_the_appliance_on_the_channel_that_works(self) -> None:
        commands = self._commands()
        entity = self._entity(commands)
        asyncio.run(entity.async_set_operation_mode("off"))
        self.assertEqual(commands["startProgram"].send_calls, 1)
        self.assertEqual(commands["startProgram"].parameters["onOffStatus"].value, "0")
        # The pinned settings command is never used for this.
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_the_real_categories_pin_the_running_mode(self) -> None:
        # auto 1, eco 2, elec 3, vac 4 -- read off the schema, not written down here.
        from custom_components.addhon.program_options import startprogram_machmode_map

        commands = self._commands()
        self.assertEqual(
            startprogram_machmode_map(FakeAppliance(commands)),
            {"1": "auto", "2": "eco", "3": "elec", "4": "vac"},
        )

    def test_a_self_entered_holiday_reads_as_holiday(self) -> None:
        # The 2026-08-18 capture: machMode 4 while the program enum stayed `auto`.
        commands = self._commands()
        entity = self._entity(
            commands, attributes=_attrs(machMode="4", **{"startProgram.program": "auto"})
        )
        self.assertEqual(entity.current_operation, "vac")
        self.assertIs(entity.is_away_mode_on, True)

    def test_the_setpoint_snaps_onto_the_real_grid(self) -> None:
        # The shadow reports an off-grid 62.8 on a range[35,75,1]; every dial press would
        # otherwise produce a value the range setter refuses.
        from homeassistant.const import ATTR_TEMPERATURE

        commands = self._commands()
        entity = self._entity(commands, attributes=_attrs(tempSel="62.8"))
        self.assertEqual(entity.target_temperature, 62.8)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 63.8}))
        self.assertEqual(commands["startProgram"].parameters["tempSel"].value, 64)


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


class ScheduledVacationHoldTest(unittest.TestCase):
    """A window scheduled by dates (grSetVacDate) never touches the program: on the
    real HP250M7C-F9 machMode flipped 1 -> 4 the day the window started while
    startProgram.program stayed "auto" (two live dumps around 2026-08-18). The read
    side must therefore report holiday from machMode, not only from the vac program.
    """

    def test_machmode_hold_reports_away_on(self) -> None:
        added, _ = _one(attributes=_attrs(machMode="4"))
        self.assertIs(added[0].is_away_mode_on, True)

    def test_machmode_hold_outranks_the_program_in_the_state(self) -> None:
        # The shadow program stays "eco": the state must say vac anyway -- the
        # device is holidaying whatever it will run afterwards.
        added, _ = _one(attributes=_attrs(machMode="4"))
        self.assertEqual(added[0].current_operation, "vac")

    def test_a_normal_machmode_is_not_away(self) -> None:
        added, _ = _one(attributes=_attrs(machMode="1"))
        self.assertIs(added[0].is_away_mode_on, False)


class RunningModeTest(unittest.TestCase):
    """machMode reports which program is RUNNING; the enum reports which is configured.

    The pairing comes from the device schema: each startProgram category pins its own
    machMode (HP250M7C-F9: auto 1, eco 2, elec 3, vac 4), and the appliance's accepted
    commands carry exactly that (a PROGRAMS.HW.ECO command with machMode "2"). Nothing
    is hard-coded, so a model that numbers its modes differently maps itself.
    """

    def test_the_running_program_wins_over_the_configured_one(self) -> None:
        # Configured `eco`, running `auto`: the state describes the appliance.
        added, _ = _one(attributes=_attrs(machMode="1"))
        self.assertEqual(added[0].current_operation, "heat_pump")

    def test_they_agree_in_the_ordinary_case(self) -> None:
        added, _ = _one(attributes=_attrs(machMode="2"))
        self.assertEqual(added[0].current_operation, "eco")

    def test_every_category_maps_itself(self) -> None:
        for mach_mode, expected in (("1", "heat_pump"), ("2", "eco"), ("3", "electric")):
            with self.subTest(machMode=mach_mode):
                added, _ = _one(attributes=_attrs(machMode=mach_mode))
                self.assertEqual(added[0].current_operation, expected)

    def test_without_categories_the_program_enum_still_answers(self) -> None:
        # A model whose startProgram is not split per program has no machMode pairing to
        # read; the behaviour must be exactly what it was.
        added, _ = _one(
            commands=_hw_commands(with_categories=False),
            attributes=_attrs(machMode="1"),
        )
        self.assertEqual(added[0].current_operation, "eco")

    def test_an_unmapped_machmode_falls_back_rather_than_guessing(self) -> None:
        added, _ = _one(attributes=_attrs(machMode="9"))
        self.assertEqual(added[0].current_operation, "eco")

    def test_an_ambiguous_map_is_refused_whole(self) -> None:
        # Two programs claiming one machMode is not knowledge; the map is dropped and the
        # program enum answers instead.
        commands = _hw_commands()
        for sibling in commands["startProgram"].categories.values():
            sibling.parameters["machMode"] = FixedParam("1")
        added, _ = _one(commands=commands, attributes=_attrs(machMode="1"))
        self.assertEqual(added[0].current_operation, "eco")

    def test_the_write_envelope_still_uses_the_configured_program(self) -> None:
        # The read follows the appliance, the WRITE follows the setting: that is what the
        # app sends (its selected program), and re-asserting a self-entered holiday would
        # make it the configured one.
        from homeassistant.const import ATTR_TEMPERATURE

        commands = _hw_commands("auto")
        added = asyncio.run(
            _build(
                "HW",
                FakeAppliance(commands),
                _attrs(machMode="4", **{"startProgram.program": "eco"}),
                client=FakeClient(),
            )
        )
        entity = added[0]
        self.assertEqual(entity.current_operation, "vac")
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 58.0}))
        self.assertEqual(
            commands["startProgram"].parameters["program"].value, "eco"
        )

    def test_power_off_still_wins_over_the_hold(self) -> None:
        added, _ = _one(attributes=_attrs(machMode="4", onOffStatus="0"))
        self.assertEqual(added[0].current_operation, "off")
        self.assertIs(added[0].is_away_mode_on, True)

    def test_away_off_from_a_hold_restarts_the_untouched_program(self) -> None:
        # The program never left "eco", so the exit that changes nothing else is
        # re-starting THAT program -- not the first normal code.
        added, commands = _one(attributes=_attrs(machMode="4"), client=FakeClient())
        asyncio.run(added[0].async_turn_away_mode_off())
        self.assertEqual(commands["startProgram"].parameters["program"].value, "eco")

    def test_binary_sensor_shares_the_derivation(self) -> None:
        # The `vacation_active` binary reads the SAME attribute through the SAME
        # hw_values helper as the entity, so the two surfaces can never disagree.
        from custom_components.addhon import binary_sensor
        from custom_components.addhon.hw_values import (
            HW_MACH_MODE_ATTR,
            hw_vacation_active,
        )

        desc = next(
            d
            for d in binary_sensor._HEAT_PUMP_BINARY
            if d.key == "vacation_active"
        )
        self.assertEqual(desc.attr_key, HW_MACH_MODE_ATTR)
        self.assertIs(desc.value_fn, hw_vacation_active)
        self.assertIs(hw_vacation_active("4"), True)
        self.assertIs(hw_vacation_active(4), True)
        self.assertIs(hw_vacation_active("1"), False)
        self.assertIsNone(hw_vacation_active(None))


if __name__ == "__main__":
    unittest.main()
