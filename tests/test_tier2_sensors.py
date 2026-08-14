# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Tier 2 read-only appliance types (capability-gated).

Tier 2 adds sensor + binary_sensor support for the appliance types that are
mapped but not validated on real devices: fridge/freezer (REF/FR/FRE), oven
(OV), dishwasher (DW), wine cellar (WC), hob (IH/HOB), hood (HO), coffee/kettle
(KT), water heater (WH) and robot vacuum (RVC). Every Tier 2 description is
CAPABILITY-GATED: the entity is created only when the device actually exposes
its attr_key. Historic types (AC/WM/WD/TD) stay ungated.

Stdlib unittest with inline Home Assistant stubs (real frozen kw_only dataclass
descriptions so the Hon* subclasses work). No real Home Assistant install
required. Stubs use getattr-guards so they coexist with the other test modules'
stubs in a shared pytest process.
"""
from __future__ import annotations

import dataclasses
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
    exceptions.ConfigEntryNotReady = getattr(exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exceptions.ConfigEntryAuthFailed = getattr(exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

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

        @property
        def available(self) -> bool:
            return getattr(self.coordinator, "last_update_success", True)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    # Force-assign (not getattr-default): another test module may have already
    # registered a CoordinatorEntity WITHOUT `available`, and ConnectivityBinaryTest
    # relies on super().available resolving regardless of suite order. This stub is a
    # superset of the minimal ones in test_program_select / test_number_setpoints /
    # test_entity_translation_keys, so taking over the shared class is safe (mirrors
    # test_entity_availability.py).
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")

    # ── sensor platform stub ──────────────────────────────────────────────
    sensor_mod = _mod("homeassistant.components.sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: object | None = None
        state_class: object | None = None
        options: object | None = None

    class SensorEntity:
        pass

    class SensorDeviceClass:
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"
        ENERGY = "energy"
        WATER = "water"
        DURATION = "duration"
        PM25 = "pm25"
        PM10 = "pm10"
        CO2 = "carbon_dioxide"
        CO = "carbon_monoxide"
        AQI = "aqi"
        VOLATILE_ORGANIC_COMPOUNDS_PARTS = "volatile_organic_compounds_parts"
        WEIGHT = "weight"
        BATTERY = "battery"
        POWER = "power"
        ENUM = "enum"
        TIMESTAMP = "timestamp"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", SensorEntity)
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", SensorDeviceClass)
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", SensorStateClass)

    # ── binary_sensor platform stub ───────────────────────────────────────
    binary_mod = _mod("homeassistant.components.binary_sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None

    class BinarySensorEntity:
        pass

    class BinarySensorDeviceClass:
        DOOR = "door"
        PROBLEM = "problem"
        RUNNING = "running"
        OCCUPANCY = "occupancy"
        LIGHT = "light"
        CONNECTIVITY = "connectivity"
        HEAT = "heat"

    binary_mod.BinarySensorEntityDescription = getattr(binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription)
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", BinarySensorEntity)
    binary_mod.BinarySensorDeviceClass = getattr(binary_mod, "BinarySensorDeviceClass", BinarySensorDeviceClass)

    # ── number platform stub (so importing custom_components.addhon.number works
    #    standalone, not only when an earlier test module installed it) ─────────
    number_mod = _mod("homeassistant.components.number")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        native_unit_of_measurement: str | None = None
        mode: object | None = None

    class NumberEntity:
        pass

    class NumberDeviceClass:
        TEMPERATURE = "temperature"

    class NumberMode:
        BOX = "box"
        AUTO = "auto"
        SLIDER = "slider"

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", NumberEntity)
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", NumberDeviceClass)
    number_mod.NumberMode = getattr(number_mod, "NumberMode", NumberMode)

    const = _mod("homeassistant.const")

    class UnitOfEnergy:
        KILO_WATT_HOUR = "kWh"

    class UnitOfVolume:
        LITERS = "L"

    class UnitOfTime:
        MINUTES = "min"
        SECONDS = "s"

    class UnitOfTemperature:
        CELSIUS = "°C"

    class UnitOfMass:
        GRAMS = "g"
        KILOGRAMS = "kg"

    const.UnitOfEnergy = getattr(const, "UnitOfEnergy", UnitOfEnergy)
    const.UnitOfVolume = getattr(const, "UnitOfVolume", UnitOfVolume)
    const.UnitOfTime = getattr(const, "UnitOfTime", UnitOfTime)
    const.UnitOfTemperature = getattr(const, "UnitOfTemperature", UnitOfTemperature)
    const.UnitOfMass = getattr(const, "UnitOfMass", UnitOfMass)
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    components.sensor = sensor_mod
    components.binary_sensor = binary_mod


_install_homeassistant_stubs()


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _sensor_keys(app_type: str) -> list[str]:
    from custom_components.addhon.sensor import SENSORS

    return [d.key for d in SENSORS.get(app_type, ())]


def _binary_keys(app_type: str) -> list[str]:
    from custom_components.addhon.binary_sensor import BINARY_SENSORS

    return [d.key for d in BINARY_SENSORS.get(app_type, ())]


async def _build_sensors(app_type: str, attributes: dict) -> list:
    from custom_components.addhon import sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    # Drop the account-level diagnostic entities so the per-appliance assertions
    # below see only the appliance sensors.
    return [e for e in added if not getattr(e, "_addhon_account", False)]


async def _build_binary(app_type: str, attributes: dict) -> list:
    from custom_components.addhon import binary_sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await binary_sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    # Drop the account-level diagnostic entities (see _build_sensors).
    return [e for e in added if not getattr(e, "_addhon_account", False)]


class Tier2TableTest(unittest.TestCase):
    def test_water_heater_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("WH"),
            ["water_temp", "temp_inlet", "temp_outlet", "power", "water_volume",
             "heating_remaining", "program_phase"],
        )

    def test_vacuum_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("RVC"),
            ["battery", "state", "remaining_time", "power_mode", "last_work_area",
             "total_work_area", "errors"],
        )

    def test_dishwasher_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("DW"),
            ["state", "program_name", "remaining_time", "delay_time", "salt_level",
             "rinse_aid_level", "water_hardness", "wash_temperature", "errors"],
        )

    def test_oven_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("OV"),
            ["state", "program_name", "temp_cavity", "remaining_time",
             "delay_time", "program_duration", "probe_temp_1", "probe_temp_2",
             "errors"],
        )

    def test_wine_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("WC"),
            ["state", "program_name", "temp_ambient", "temp_zone1", "temp_zone2",
             "humidity_zone1", "humidity_zone2", "remaining_time", "errors"],
        )

    def test_fr_and_fre_alias_cooling(self) -> None:
        self.assertEqual(_sensor_keys("REF"), _sensor_keys("FR"))
        self.assertEqual(_sensor_keys("REF"), _sensor_keys("FRE"))

    def test_hob_alias(self) -> None:
        self.assertEqual(_sensor_keys("IH"), _sensor_keys("HOB"))
        self.assertEqual(_binary_keys("IH"), _binary_keys("HOB"))

    def test_all_tier2_descriptions_are_gated(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        for app_type in ("REF", "FR", "FRE", "OV", "DW", "WC", "IH", "HOB", "HO", "KT", "WH", "RVC"):
            for d in SENSORS[app_type]:
                self.assertTrue(d.gated, f"{app_type}/{d.key} must be gated")

    def test_historic_core_not_gated_only_optionals_gated(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        # Historic types keep their CORE sensors always-created (gated=False); only
        # the optional gvigroux-harvested add-ons (air-quality / auto-dose) are gated.
        expected_gated = {
            "AC": {"pm10", "voc", "co", "air_quality"},
            "WM": {"current_wash_cycle", "remaining_rinses", "detergent_level",
                   "detergent_weight", "softener_weight", "estimated_weight"},
            "WD": {"current_wash_cycle", "remaining_rinses", "detergent_level",
                   "detergent_weight", "softener_weight", "estimated_weight"},
            "TD": set(),
        }
        for app_type, gated_keys in expected_gated.items():
            actual = {d.key for d in SENSORS[app_type] if d.gated}
            self.assertEqual(actual, gated_keys, f"{app_type} gated set mismatch")

    def test_hob_binary_has_six_pan_zones(self) -> None:
        self.assertEqual(
            _binary_keys("IH"),
            [f"pan_zone{z}" for z in range(1, 7)],
        )


class Tier2GatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooling_creates_only_reported_attrs(self) -> None:
        added = await _build_sensors("REF", {"tempZ1": "4", "tempEnv": "22", "humidityEnv": "55"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_temp_zone1", "x-1_temp_ambient", "x-1_humidity_ambient"},
        )

    async def test_no_attrs_means_no_entities(self) -> None:
        added = await _build_sensors("REF", {})
        self.assertEqual(added, [])

    async def test_unknown_type_no_entities(self) -> None:
        added = await _build_sensors("ZZ", {"tempZ1": "4"})
        self.assertEqual(added, [])

    async def test_oven_state_decodes_machine_mode(self) -> None:
        added = await _build_sensors("OV", {"machMode": "2"})
        state = next(e for e in added if e._attr_unique_id == "x-1_state")
        self.assertEqual(state.native_value, "running")

    async def test_dishwasher_salt_level_decodes(self) -> None:
        added = await _build_sensors("DW", {"saltStatus": "1", "rinseAidStatus": "0"})
        salt = next(e for e in added if e._attr_unique_id == "x-1_salt_level")
        rinse = next(e for e in added if e._attr_unique_id == "x-1_rinse_aid_level")
        self.assertEqual(salt.native_value, "low")
        self.assertEqual(rinse.native_value, "ok")

    async def test_vacuum_state_power_battery(self) -> None:
        added = await _build_sensors("RVC", {"prPhase": "6", "power": "1", "batteryStatus": "80"})
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_id["x-1_state"].native_value, "charging")
        self.assertEqual(by_id["x-1_power_mode"].native_value, "turbo")
        self.assertEqual(by_id["x-1_battery"].native_value, 80.0)

    async def test_unknown_enum_value_is_none(self) -> None:
        # ENUM sensors must not emit out-of-options values: an unknown code -> None
        # (the sensor reports "unknown" rather than a raw label).
        added = await _build_sensors("RVC", {"prPhase": "99"})
        state = next(e for e in added if e._attr_unique_id == "x-1_state")
        self.assertIsNone(state.native_value)

    async def test_dryer_temp_level(self) -> None:
        # TD gains a temperature-level sensor (tempLevel); live-confirmed on HD100.
        added = await _build_sensors("TD", {"tempLevel": "4", "machMode": "1"})
        tl = next(e for e in added if e._attr_unique_id == "x-1_temp_level")
        self.assertEqual(tl.native_value, 4.0)

    async def test_washer_stain_type_decodes(self) -> None:
        # WM stain_type ENUM: code -> machine key, 0 -> none, unknown -> None.
        # 9/13/15 corrected to the app's stainOptions (were ice_cream/rust/perfume).
        cases = (
            ("1", "wine"), ("0", "none"), ("26", "fruit"), ("99", None),
            ("9", "chocolate"), ("13", "chili_oil"), ("15", "color_pencil"),
        )
        for raw, expected in cases:
            added = await _build_sensors("WM", {"stainType": raw})
            st = next(e for e in added if e._attr_unique_id == "x-1_stain_type")
            self.assertEqual(st.native_value, expected)

    async def test_washer_state_decodes(self) -> None:
        # WM state ENUM uses the authoritative MachineMode: 2=running, 3=paused.
        for raw, expected in (
            ("0", "idle"), ("1", "selection"), ("2", "running"),
            ("3", "paused"), ("7", "finished"), ("99", None),
        ):
            added = await _build_sensors("WM", {"machMode": raw})
            st = next(e for e in added if e._attr_unique_id == "x-1_state")
            self.assertEqual(st.native_value, expected)


class ConstMapTest(unittest.TestCase):
    def test_ac_fan_map_auto_is_5(self) -> None:
        from custom_components.addhon.const import AC_FAN_MAP, AC_FAN_MAP_REVERSE

        self.assertEqual(AC_FAN_MAP.get("5"), "auto")
        self.assertNotIn("0", AC_FAN_MAP)  # the device rejects windSpeed 0
        self.assertEqual(AC_FAN_MAP_REVERSE["auto"], "5")
        self.assertEqual(AC_FAN_MAP_REVERSE["high"], "1")

    def test_wm_state_map_authoritative_codes(self) -> None:
        from custom_components.addhon.const import WM_STATE_MAP

        # Authoritative MachineMode semantics (decomp): 1=selection, 2=running,
        # 3=paused, 7=finished; "half_load" is a program option, never a state.
        self.assertEqual(WM_STATE_MAP["1"], "selection")
        self.assertEqual(WM_STATE_MAP["2"], "running")
        self.assertEqual(WM_STATE_MAP["3"], "paused")
        self.assertEqual(WM_STATE_MAP["7"], "finished")
        self.assertNotIn("half_load", WM_STATE_MAP.values())

    def test_stain_map_full_table(self) -> None:
        from custom_components.addhon.const import STAIN_TYPE_MAP

        # Locks the entire app stainOptions table (decomp.txt:977322-977422).
        self.assertEqual(STAIN_TYPE_MAP, {
            "0": "none", "1": "wine", "2": "grass", "3": "soil", "4": "blood",
            "5": "milk", "6": "cooking_oil", "7": "tea", "8": "coffee",
            "9": "chocolate", "10": "lip_gloss", "11": "curry", "12": "milk_tea",
            "13": "chili_oil", "14": "blue_ink", "15": "color_pencil",
            "16": "shoe_cream", "17": "oil_pastel", "18": "blueberry", "19": "sweat",
            "20": "egg", "21": "ketchup", "22": "baby_food", "23": "soy_sauce",
            "24": "bean_paste", "25": "chili_sauce", "26": "fruit",
        })


class PauseSwitchTest(unittest.IsolatedAsyncioTestCase):
    def _switch(self, mach_mode: str):
        sw = _mod("homeassistant.components.switch")
        sw.SwitchEntity = getattr(sw, "SwitchEntity", type("SwitchEntity", (), {}))
        from custom_components.addhon.switch import HonWashingMachinePauseSwitch

        data = {"x-1": {"type": "WM", "name": "Dev",
                        "attributes": {"machMode": mach_mode}, "settings": {}}}
        return HonWashingMachinePauseSwitch(FakeCoordinator(data), "x-1", None)

    def test_is_on_only_when_paused(self) -> None:
        self.assertTrue(self._switch("3").is_on)   # 3 = PAUSE_MODE
        self.assertFalse(self._switch("2").is_on)  # 2 = EXECUTION (running)
        self.assertFalse(self._switch("0").is_on)


class Tier2BinaryGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooling_binary_gating(self) -> None:
        added = await _build_binary("REF", {"doorStatusZ1": "1", "icemakerOnOffStatus": "0"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_door_zone1", "x-1_ice_maker", "x-1_connectivity"},
        )
        door = next(e for e in added if e._attr_unique_id == "x-1_door_zone1")
        self.assertTrue(door.is_on)

    async def test_cooling_binary_mode_flags(self) -> None:
        # Read-only active-mode flags (quickModeZ1/Z2, intelligenceMode, holidayMode),
        # capability-gated and decoded as 0/1. Live-confirmed present on the real fridge.
        added = await _build_binary(
            "REF",
            {"quickModeZ1": "1", "quickModeZ2": "0", "intelligenceMode": "1", "holidayMode": "0"},
        )
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(
            set(by_id),
            {"x-1_quick_cool", "x-1_quick_freeze", "x-1_auto_set",
             "x-1_holiday_mode", "x-1_connectivity"},
        )
        self.assertTrue(by_id["x-1_quick_cool"].is_on)
        self.assertFalse(by_id["x-1_quick_freeze"].is_on)
        self.assertTrue(by_id["x-1_auto_set"].is_on)
        self.assertFalse(by_id["x-1_holiday_mode"].is_on)

    async def test_hob_binary_only_present_zones(self) -> None:
        added = await _build_binary("IH", {"panStatusZ1": "1", "panStatusZ3": "0"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_pan_zone1", "x-1_pan_zone3", "x-1_connectivity"},
        )

    async def test_water_heater_binary(self) -> None:
        added = await _build_binary("WH", {"lockStatus": "1"})
        self.assertEqual({e._attr_unique_id for e in added}, {"x-1_child_lock", "x-1_connectivity"})
        lock = next(e for e in added if e._attr_unique_id == "x-1_child_lock")
        self.assertTrue(lock.is_on)

    async def test_ac_binary_gating(self) -> None:
        # AC gains a per-type binary set (filter change + formaldehyde cleaning),
        # capability-gated like all binary sensors. Live-confirmed on the real AC.
        added = await _build_binary("AC", {"filterChangeStatusLocal": "1", "ch2oCleaningStatus": "0"})
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(
            set(by_id), {"x-1_filter_change", "x-1_ch2o_cleaning", "x-1_connectivity"}
        )
        self.assertTrue(by_id["x-1_filter_change"].is_on)
        self.assertFalse(by_id["x-1_ch2o_cleaning"].is_on)


class ConnectivityBinaryTest(unittest.IsolatedAsyncioTestCase):
    async def _conn(self, attributes):
        added = await _build_binary("AC", attributes)  # AC: no per-type set
        return next(e for e in added if e._attr_unique_id == "x-1_connectivity")

    async def test_created_for_type_without_pertype_set(self) -> None:
        added = await _build_binary("AC", {"available": True})
        self.assertEqual({e._attr_unique_id for e in added}, {"x-1_connectivity"})

    async def test_is_on_reflects_available(self) -> None:
        self.assertTrue((await self._conn({"available": True})).is_on)
        self.assertFalse((await self._conn({"available": False})).is_on)
        self.assertIsNone((await self._conn({})).is_on)

    async def test_stays_available_when_device_disconnected(self) -> None:
        # the connectivity sensor must stay AVAILABLE so it can report 'disconnected'
        conn = await self._conn({"available": False})
        self.assertTrue(conn.available)
        self.assertFalse(conn.is_on)


class GvigrouxImportTest(unittest.IsolatedAsyncioTestCase):
    """Live-tested mapping items adopted from gvigroux/hon (real-device evidence)."""

    async def test_oven_gains_program_delay_errors(self) -> None:
        added = await _build_sensors("OV", {
            "machMode": "2", "programName": "PIZZA", "delayTime": 30,
            "errors": "00", "temp": 180, "remainingTimeMM": 20,
        })
        uids = {e._attr_unique_id for e in added}
        self.assertIn("x-1_program_name", uids)
        self.assertIn("x-1_delay_time", uids)
        self.assertIn("x-1_errors", uids)

    async def test_oven_preheat_binary(self) -> None:
        added = await _build_binary("OV", {"preheatStatus": "1"})
        preheat = next(e for e in added if e._attr_unique_id == "x-1_preheat")
        self.assertTrue(preheat.is_on)

    def test_oven_number_fallback_is_oven_range(self) -> None:
        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.number import NUMBERS

        target = {d.key: d for d in NUMBERS[APPLIANCE_OV]}["target_temp"]
        self.assertEqual(
            (target.fallback_min, target.fallback_max, target.fallback_step),
            (50.0, 280.0, 5.0),
        )

    async def test_dishwasher_wash_temp_reads_temp_or_temperature(self) -> None:
        # Both key variants build the sensor and yield the value (gate/read both).
        for attrs in ({"temp": "45"}, {"temperature": "45"}):
            added = await _build_sensors("DW", attrs)
            wt = next(e for e in added if e._attr_unique_id == "x-1_wash_temperature")
            self.assertEqual(wt.native_value, 45.0)

    def test_oven_delay_time_is_duration_minutes(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfTime

        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.sensor import SENSORS

        d = {x.key: x for x in SENSORS[APPLIANCE_OV]}["delay_time"]
        self.assertEqual(d.device_class, SensorDeviceClass.DURATION)
        self.assertEqual(d.native_unit_of_measurement, UnitOfTime.MINUTES)

    def test_oven_preheat_device_class_is_heat(self) -> None:
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.addhon.binary_sensor import BINARY_SENSORS
        from custom_components.addhon.const import APPLIANCE_OV

        d = {x.key: x for x in BINARY_SENSORS[APPLIANCE_OV]}["preheat"]
        self.assertEqual(d.device_class, BinarySensorDeviceClass.HEAT)

    async def test_wine_cooler_zone1_temp_and_humidity(self) -> None:
        added = await _build_sensors("WC", {
            "temp": 12, "tempZ2": 8, "humidityZ1": 60, "humidityZ2": 65,
        })
        by_uid = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_uid["x-1_temp_zone1"].native_value, 12.0)  # reads `temp`
        self.assertIn("x-1_temp_zone2", by_uid)
        self.assertIn("x-1_humidity_zone1", by_uid)
        self.assertIn("x-1_humidity_zone2", by_uid)

    async def test_wine_cooler_zone1_ignores_tempz1(self) -> None:
        # zone1 actual temp is `temp`, not `tempZ1` (which does not exist for WC).
        added = await _build_sensors("WC", {"tempZ1": 99})
        self.assertNotIn("x-1_temp_zone1", {e._attr_unique_id for e in added})

    # --- gvigroux P1 harvest: air-quality / auto-dose / options (all gated) ---

    async def test_ac_air_quality_appears_only_when_reported(self) -> None:
        added = await _build_sensors("AC", {
            "pm10ValueIndoor": "6", "vocValueIndoor": "1", "coLevel": "0",
            "airQuality": "2",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        for k in ("pm10", "voc", "co", "air_quality"):
            self.assertIn(f"x-1_{k}", by_uid)
        self.assertEqual(by_uid["x-1_air_quality"].native_value, 2.0)
        # absent when the device does not report them (gated)
        uids2 = {e._attr_unique_id for e in await _build_sensors("AC", {})}
        for k in ("pm10", "voc", "co", "air_quality"):
            self.assertNotIn(f"x-1_{k}", uids2)

    def test_ac_air_quality_device_classes(self) -> None:
        # pm10 is a real mass concentration; voc/co/air_quality are level indexes
        # surfaced as plain integers (no misleading device_class/unit).
        from homeassistant.components.sensor import SensorDeviceClass

        from custom_components.addhon.const import APPLIANCE_AC
        from custom_components.addhon.sensor import SENSORS

        ac = {d.key: d for d in SENSORS[APPLIANCE_AC]}
        self.assertEqual(ac["pm10"].device_class, SensorDeviceClass.PM10)
        self.assertEqual(ac["pm10"].native_unit_of_measurement, "µg/m³")
        for k in ("voc", "co", "air_quality"):
            self.assertIsNone(ac[k].device_class, f"{k} must be class-less")
            self.assertIsNone(ac[k].native_unit_of_measurement, f"{k} must be unitless")

    async def test_dishwasher_delay_hardness_and_option_binaries(self) -> None:
        added = await _build_sensors("DW", {"delayTime": "30", "waterHard": "3"})
        uids = {e._attr_unique_id for e in added}
        self.assertIn("x-1_delay_time", uids)
        hard = next(e for e in added if e._attr_unique_id == "x-1_water_hardness")
        self.assertEqual(hard.native_value, 3.0)
        bins = await _build_binary("DW", {
            "extraDry": "1", "halfLoad": "0", "openDoor": "1", "ecoExpress": "0",
        })
        buids = {e._attr_unique_id for e in bins}
        for k in ("extra_dry", "half_load", "auto_open_door", "eco_express"):
            self.assertIn(f"x-1_{k}", buids)
        extra = next(e for e in bins if e._attr_unique_id == "x-1_extra_dry")
        self.assertTrue(extra.is_on)

    async def test_washer_dose_sensors_and_option_binaries(self) -> None:
        added = await _build_sensors("WM", {
            "currentWashCycle": "2", "remainingRinseIterations": "1",
            "detergentPercent": "80", "haier_DetergentWeight": "35",
            "haier_SoftenerWeight": "20",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        for k in ("current_wash_cycle", "remaining_rinses", "detergent_level",
                  "detergent_weight", "softener_weight"):
            self.assertIn(f"x-1_{k}", by_uid)
        self.assertEqual(by_uid["x-1_detergent_weight"].native_value, 35.0)
        self.assertEqual(by_uid["x-1_softener_weight"].native_value, 20.0)
        bins = {e._attr_unique_id: e for e in await _build_binary("WM", {
            "nightWashStatus": "1", "steamStatus": "0", "energySavingStatus": "1",
        })}
        self.assertTrue(bins["x-1_night_wash"].is_on)
        self.assertFalse(bins["x-1_steam"].is_on)
        self.assertTrue(bins["x-1_energy_saving"].is_on)

    async def test_wine_cooler_state_program_errors(self) -> None:
        added = await _build_sensors("WC", {
            "machMode": "2", "programName": "RED", "errors": "00",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_uid["x-1_state"].native_value, "running")  # MACHINE_MODE_MAP
        self.assertEqual(by_uid["x-1_program_name"].native_value, "RED")
        self.assertEqual(by_uid["x-1_errors"].native_value, "00")

    async def test_wine_cooler_state_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("WC", {})}
        for k in ("state", "program_name", "errors"):
            self.assertNotIn(f"x-1_{k}", uids)

    async def test_oven_program_duration(self) -> None:
        added = await _build_sensors("OV", {"prTime": "2700"})
        pd = next(e for e in added if e._attr_unique_id == "x-1_program_duration")
        self.assertEqual(pd.native_value, 2700.0)

    def test_oven_program_duration_is_seconds(self) -> None:
        # prTime is seconds (range 1..86395), NOT minutes.
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfTime

        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.sensor import SENSORS

        pd = {d.key: d for d in SENSORS[APPLIANCE_OV]}["program_duration"]
        self.assertEqual(pd.native_unit_of_measurement, UnitOfTime.SECONDS)
        self.assertEqual(pd.device_class, SensorDeviceClass.DURATION)

    async def test_dishwasher_options_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("DW", {})}
        for k in ("delay_time", "water_hardness"):
            self.assertNotIn(f"x-1_{k}", uids)
        buids = {e._attr_unique_id for e in await _build_binary("DW", {})}
        for k in ("extra_dry", "half_load", "auto_open_door", "eco_express"):
            self.assertNotIn(f"x-1_{k}", buids)

    async def test_washer_dose_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("WM", {})}
        for k in ("current_wash_cycle", "remaining_rinses", "detergent_level",
                  "detergent_weight", "softener_weight"):
            self.assertNotIn(f"x-1_{k}", uids)
        buids = {e._attr_unique_id for e in await _build_binary("WM", {})}
        for k in ("night_wash", "steam", "energy_saving"):
            self.assertNotIn(f"x-1_{k}", buids)

    # --- Wave 1/2 harvest: estimated weight / remote control / mean water
    #     consumption (all gated) ---

    async def test_estimated_weight_gated_with_fallback(self) -> None:
        # actualWeight builds it; the `weight` fallback also builds it; absent when
        # neither is reported. WM and WD both carry it.
        for app_type in ("WM", "WD"):
            added = await _build_sensors(app_type, {"actualWeight": "3.5"})
            ew = next(e for e in added if e._attr_unique_id == "x-1_estimated_weight")
            self.assertEqual(ew.native_value, 3.5)
            added = await _build_sensors(app_type, {"weight": "4"})
            ew = next(e for e in added if e._attr_unique_id == "x-1_estimated_weight")
            self.assertEqual(ew.native_value, 4.0)  # reads the `weight` fallback
            uids = {e._attr_unique_id for e in await _build_sensors(app_type, {})}
            self.assertNotIn("x-1_estimated_weight", uids)

    def test_estimated_weight_device_class_kg(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfMass

        from custom_components.addhon.const import APPLIANCE_WM
        from custom_components.addhon.sensor import SENSORS

        d = {x.key: x for x in SENSORS[APPLIANCE_WM]}["estimated_weight"]
        self.assertEqual(d.device_class, SensorDeviceClass.WEIGHT)
        self.assertEqual(d.native_unit_of_measurement, UnitOfMass.KILOGRAMS)
        self.assertEqual(d.attr_fallbacks, ("weight",))

    async def test_remote_control_universal_gated(self) -> None:
        # Present on ANY type that reports remoteCtrValid (AC has no per-type set;
        # REF is a Tier-2 type) and absent otherwise. is_on follows "1".
        for app_type in ("AC", "REF"):
            added = await _build_binary(app_type, {"remoteCtrValid": "1"})
            rc = next(e for e in added if e._attr_unique_id == "x-1_remote_control")
            self.assertTrue(rc.is_on)
            uids = {e._attr_unique_id for e in await _build_binary(app_type, {})}
            self.assertNotIn("x-1_remote_control", uids)

    async def test_mean_water_consumption(self) -> None:
        # WM/WD only, gated on BOTH source attrs; value = water/(cycles-1).
        for app_type in ("WM", "WD"):
            added = await _build_sensors(
                app_type, {"totalWaterUsed": "100", "totalWashCycle": "6"})
            mw = next(e for e in added if e._attr_unique_id == "x-1_mean_water_consumption")
            self.assertEqual(mw.native_value, 20.0)  # 100 / (6-1)
        # <=0 denominator (first cycle) -> None, not a divide-by-zero
        added = await _build_sensors("WM", {"totalWaterUsed": "100", "totalWashCycle": "1"})
        mw = next(e for e in added if e._attr_unique_id == "x-1_mean_water_consumption")
        self.assertIsNone(mw.native_value)

    async def test_mean_water_absent_without_both_attrs_or_on_dryer(self) -> None:
        # Needs BOTH attrs; the tumble dryer never builds it (no water).
        for attrs in ({"totalWaterUsed": "100"}, {"totalWashCycle": "6"}):
            uids = {e._attr_unique_id for e in await _build_sensors("WM", attrs)}
            self.assertNotIn("x-1_mean_water_consumption", uids)
        uids = {e._attr_unique_id for e in await _build_sensors(
            "TD", {"totalWaterUsed": "100", "totalWashCycle": "6"})}
        self.assertNotIn("x-1_mean_water_consumption", uids)


class HeatPumpEnergyCountersTest(unittest.IsolatedAsyncioTestCase):
    """HW period counters, against the real HP250M7C-F9 diagnostics dump.

    Captured 2026-07-26 08:00 (device clock), so July == slot index 6 and the Year
    series' last element is the year to date.
    """

    # Verbatim from the dump.
    DUMP = {
        "date": "2026-07-26",
        "weekDay": 7,
        "energyConsumptionMonthCp": "0;0;0;0;0;0;11;0;0;0;0;0",
        "energyConsumptionMonthEc": "0;0;0;0;0;0;8;0;0;0;0;0",
        "accumulatedHeatMonth": "0;0;0;0;0;0;125;0;0;0;0;0",
        "energyConsumptionYearCp": "0;0;0;0;11",
        "energyConsumptionYearEc": "0;0;0;0;8",
        "accumulatedHeatYear": "0;0;0;0;125",
    }

    async def _values(self, attributes: dict) -> dict:
        added = await _build_sensors("HW", attributes)
        return {e.entity_description.key: e.native_value for e in added}

    async def test_month_slot_indexed_by_device_clock(self) -> None:
        values = await self._values(dict(self.DUMP))
        self.assertEqual(values["compressor_energy_month"], 11.0)
        self.assertEqual(values["heater_energy_month"], 8.0)
        self.assertEqual(values["heat_output_month"], 125.0)

    async def test_month_slot_follows_the_device_not_the_host(self) -> None:
        # Same series, device clock rewound to March -> slot 2, which is empty. If the
        # picker used the HOST date this would still report July's 11.
        march = dict(self.DUMP, date="2026-03-15")
        self.assertEqual((await self._values(march))["compressor_energy_month"], 0.0)

    async def test_month_falls_back_when_device_clock_absent(self) -> None:
        # No `date` attribute: still resolves a slot rather than going unknown.
        no_date = {k: v for k, v in self.DUMP.items() if k != "date"}
        self.assertIsNotNone((await self._values(no_date))["compressor_energy_month"])

    async def test_year_is_the_month_sum_and_matches_the_dump_invariant(self) -> None:
        # The whole point of reading the Month series for the yearly counters: the sum
        # must reproduce the Year series' last element the device itself reports.
        values = await self._values(dict(self.DUMP))
        self.assertEqual(values["compressor_energy_year"], 11.0)
        self.assertEqual(values["heater_energy_year"], 8.0)
        self.assertEqual(values["heat_output_year"], 125.0)

    async def test_year_survives_a_window_that_never_slides(self) -> None:
        # A device that does not slide its 5-year window on 1 January would freeze a
        # parts[-1] reading on the old year. Reading the Month series is immune, so a
        # stale (or missing) Year series must not affect the result.
        stale = {k: v for k, v in self.DUMP.items() if not k.endswith(("YearCp", "YearEc"))}
        stale["accumulatedHeatYear"] = "0;0;0;0;0"
        values = await self._values(stale)
        self.assertEqual(values["compressor_energy_year"], 11.0)
        self.assertEqual(values["heat_output_year"], 125.0)

    async def test_heat_output_is_not_offered_as_an_energy_source(self) -> None:
        # Electricity keeps the ENERGY device class (Energy dashboard selectable);
        # thermal output must NOT, or adding it there inflates consumption ~6x.
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

        added = {e.entity_description.key: e.entity_description
                 for e in await _build_sensors("HW", dict(self.DUMP))}
        for key in ("compressor_energy_month", "compressor_energy_year",
                    "heater_energy_month", "heater_energy_year"):
            self.assertEqual(added[key].device_class, SensorDeviceClass.ENERGY, key)
        for key in ("heat_output_month", "heat_output_year"):
            self.assertIsNone(added[key].device_class, key)
            # Still recorded as long-term statistics.
            self.assertEqual(added[key].state_class, SensorStateClass.TOTAL_INCREASING, key)

    async def test_malformed_series_reports_unknown(self) -> None:
        broken = dict(self.DUMP, energyConsumptionMonthCp="not;a;series")
        values = await self._values(broken)
        self.assertIsNone(values["compressor_energy_month"])
        self.assertIsNone(values["compressor_energy_year"])

    async def test_total_energy_sums_both_sources_across_the_year_window(self) -> None:
        # The dump device is in its first year: total == this year's Cp + Ec.
        self.assertEqual((await self._values(dict(self.DUMP)))["total_energy"], 19.0)
        # With history in the older slots the whole window counts, not just parts[-1]:
        # Cp 3+7+0+0+11 = 21, Ec 1+0+2+0+8 = 11.
        aged = dict(self.DUMP,
                    energyConsumptionYearCp="3;7;0;0;11",
                    energyConsumptionYearEc="1;0;2;0;8")
        self.assertEqual((await self._values(aged))["total_energy"], 32.0)

    async def test_total_energy_covers_a_single_source_device(self) -> None:
        # Only Cp reported: the total is the compressor alone...
        only_cp = {k: v for k, v in self.DUMP.items() if k != "energyConsumptionYearEc"}
        self.assertEqual((await self._values(only_cp))["total_energy"], 11.0)
        # ...and only Ec still gates the sensor in through the fallback attr.
        only_ec = {k: v for k, v in self.DUMP.items() if k != "energyConsumptionYearCp"}
        self.assertEqual((await self._values(only_ec))["total_energy"], 8.0)

    async def test_total_energy_goes_unknown_when_either_series_is_corrupt(self) -> None:
        # A partial (one-source) reading would look like a meter reset to
        # TOTAL_INCREASING statistics and double-count the missing half on recovery,
        # so a present-but-corrupt series must take the WHOLE total to unknown.
        broken = dict(self.DUMP, energyConsumptionYearEc="not;a;series")
        self.assertIsNone((await self._values(broken))["total_energy"])

    async def test_total_energy_is_an_enabled_energy_dashboard_source(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

        added = {e.entity_description.key: e
                 for e in await _build_sensors("HW", dict(self.DUMP))}
        total = added["total_energy"]
        self.assertEqual(total.entity_description.device_class, SensorDeviceClass.ENERGY)
        self.assertEqual(total.entity_description.state_class,
                         SensorStateClass.TOTAL_INCREASING)
        # Unlike the per-source counters, the lifetime total registers ENABLED:
        # it is the one counter the Energy dashboard asks for.
        self.assertTrue(total._attr_entity_registry_enabled_default)
        self.assertFalse(
            added["compressor_energy_month"]._attr_entity_registry_enabled_default)


class HeatPumpHeatingStatusSensorsTest(unittest.IsolatedAsyncioTestCase):
    """`heating_status` / `heat_source`: what the appliance is DOING, as entities.

    They exist because the water_heater more-info history chart is single-entity by
    construction and plots only the two temperature lines -- an attribute, however
    faithfully recorded, can never appear there. As sensors these get their own history.
    """

    SOURCES = {
        "compressorHeatingCurrentStatus": "0",
        "electricHeatingCurrentStatus": "0",
    }

    async def _values(self, **overrides) -> dict:
        added = await _build_sensors("HW", dict(self.SOURCES, **overrides))
        return {e.entity_description.key: e.native_value for e in added}

    async def test_heating_when_a_source_runs(self) -> None:
        values = await self._values(compressorHeatingCurrentStatus="1")
        self.assertEqual(values["heating_status"], "heating")
        self.assertEqual(values["heat_source"], "compressor")

    async def test_idle_when_on_but_no_source_runs(self) -> None:
        values = await self._values()
        self.assertEqual(values["heating_status"], "idle")
        self.assertEqual(values["heat_source"], "none")

    async def test_off_wins_over_the_source_flags(self) -> None:
        # A stale source flag on a powered-off appliance must not read as heating.
        values = await self._values(onOffStatus="0", compressorHeatingCurrentStatus="1")
        self.assertEqual(values["heating_status"], "off")

    async def test_several_sources_collapse_to_multiple(self) -> None:
        values = await self._values(
            compressorHeatingCurrentStatus="1", electricHeatingCurrentStatus="1"
        )
        self.assertEqual(values["heat_source"], "multiple")

    async def test_created_from_any_single_source_flag(self) -> None:
        # Gated on the GROUP, not on the compressor alone: a model that reports only the
        # boiler still gets both sensors.
        added = await _build_sensors("HW", {"boilerHeatingCurrentStatus": "1"})
        values = {e.entity_description.key: e.native_value for e in added}
        self.assertEqual(values["heating_status"], "heating")
        self.assertEqual(values["heat_source"], "boiler")

    async def test_absent_without_heat_source_telemetry(self) -> None:
        # No confident "not heating" on a device that never reports it.
        keys = {e.entity_description.key
                for e in await _build_sensors("HW", {"tempSel": "55"})}
        self.assertNotIn("heating_status", keys)
        self.assertNotIn("heat_source", keys)

    async def test_every_reachable_value_is_a_declared_enum_option(self) -> None:
        # An ENUM sensor reporting a state outside `options` is a hard error in HA.
        from custom_components.addhon.hw_values import HW_HEAT_SOURCES

        added = {e.entity_description.key: e for e in await _build_sensors(
            "HW", dict(self.SOURCES))}
        for key in ("heating_status", "heat_source"):
            self.assertIsNotNone(added[key].entity_description.options, key)
        options = {k: set(added[k].entity_description.options)
                   for k in ("heating_status", "heat_source")}
        self.assertEqual(options["heating_status"], {"off", "idle", "heating"})
        self.assertEqual(
            options["heat_source"],
            {"none", "multiple", *(name for name, _ in HW_HEAT_SOURCES)},
        )
        # And the values actually produced stay inside them.
        for attrs in ({}, {"onOffStatus": "0"}, {"compressorHeatingCurrentStatus": "1"},
                      {"compressorHeatingCurrentStatus": "1",
                       "electricHeatingCurrentStatus": "1"}):
            values = await self._values(**attrs)
            for key in ("heating_status", "heat_source"):
                self.assertIn(values[key], options[key], (key, attrs))

    async def test_no_static_icon_so_the_per_state_icons_apply(self) -> None:
        # An entity that carries its own icon writes it into the state and overrides the
        # icon translations, pinning one symbol instead of following the running source.
        added = {e.entity_description.key: e for e in await _build_sensors(
            "HW", dict(self.SOURCES))}
        for key in ("heating_status", "heat_source"):
            self.assertIsNone(added[key].entity_description.icon, key)

    async def test_shares_the_water_heater_derivation(self) -> None:
        # One helper pair, so the sensors and the entity attributes cannot drift.
        from custom_components.addhon.hw_values import hw_action, hw_heat_source

        attributes = dict(self.SOURCES, compressorHeatingCurrentStatus="1")
        values = await self._values(compressorHeatingCurrentStatus="1")
        get_attr = attributes.get
        self.assertEqual(values["heating_status"], hw_action(get_attr))
        self.assertEqual(values["heat_source"], hw_heat_source(get_attr))


if __name__ == "__main__":
    unittest.main()
