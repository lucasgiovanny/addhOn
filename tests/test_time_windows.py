# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the water heater's schedule time entities (off-peak / quiet windows).

Ground truth, 2026-08-25 on a real HP250M7C-F9: an off-peak window written straight
through the `settings` command -- no operation name touched -- landed on the appliance
and came back as 11:00-16:00. What decides is the MANDATORY flag, not the operation the
command is named after: every schedule field is mandatory on that schema, and every
toggle that never worked is not (see hon_commands.settings_write_blocked).

Verifies:
- an entity per slot, gated on the parameter being writable rather than merely present;
- a parameter the pinned command would swallow produces NO entity;
- the state is the appliance's own clock reading, and an unusable one reads as unknown;
- the write sends HH:MM (never HH:MM:SS, which the appliance does not report back).

Stdlib unittest with inline Home Assistant stubs (no HA install required). The stubs are
getattr-guarded so they coexist with the other test modules in the pytest process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import datetime
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

    class TranslatableHomeAssistantError(Exception):
        """Mirror of HA's HomeAssistantError (accepts the translation kwargs)."""

        def __init__(
            self,
            *args,
            translation_domain=None,
            translation_key=None,
            translation_placeholders=None,
        ) -> None:
            super().__init__(*args)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders

    base_err = getattr(exceptions, "HomeAssistantError", TranslatableHomeAssistantError)
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
    update_coordinator.UpdateFailed = getattr(
        update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )

    components = _mod("homeassistant.components")
    time_mod = _mod("homeassistant.components.time")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class TimeEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        entity_category: object | None = None
        entity_registry_enabled_default: bool = True

    time_mod.TimeEntityDescription = getattr(time_mod, "TimeEntityDescription", TimeEntityDescription)
    time_mod.TimeEntity = getattr(time_mod, "TimeEntity", type("TimeEntity", (), {}))

    const = _mod("homeassistant.const")
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
    components.time = time_mod


_install_homeassistant_stubs()


class FixedParam:
    """Mimics HonParameterFixed: a settable value with NO validation. `mandatory` is what
    decides whether a pinned settings command actually applies it."""

    def __init__(self, value, mandatory: int = 1) -> None:
        self.value = value
        self.mandatory = mandatory


class RecordingCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.send_calls = 0
        self.sent = None

    @property
    def parameter_groups(self):
        return {"parameters": {k: str(p.value) for k, p in self.parameters.items()}}

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: str(p.value) for k, p in self.parameters.items()}

    async def send_parameters(self, params) -> None:
        self.send_calls += 1
        self.sent = dict(params)


class FakeAppliance:
    def __init__(self, commands, reported: dict | None = None) -> None:
        self.commands = commands
        # The device shadow, as the engine holds it (shadow_overrides and the day-mask
        # restore both read it).
        self.attributes = {"parameters": dict(reported or {})}


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


APPLIANCE_ID = "hw-1"

# The slots backing ENTITIES: PERIOD GROUP 2, slot 1 -- where a window set on the
# appliance's own panel landed (2026-08-26 capture diff). Group 1 belongs to the
# inactive off-peak tariff feature; v5.26-v5.28 wrote there and the appliance kept
# discarding it.
_SLOTS = ["opp2EcoStartTime1", "opp2EcoEndTime1"]

# Slots the appliance exposes but the platform must NOT build entities for: all of
# group 1, the remaining group-2 slots, and the quiet windows.
_UNEXPOSED = (
    [f"opp1Eco{edge}Time{n}" for n in (1, 2, 3) for edge in ("Start", "End")]
    + [f"opp2Eco{edge}Time{n}" for n in (2, 3) for edge in ("Start", "End")]
    + [f"silent{edge}Time{n}" for n in (1, 2) for edge in ("Start", "End")]
)


class MaskParam:
    """The day mask as the real schema declares it: a numeric range that CANNOT hold the
    hex value the appliance uses -- assigning "7F" raises, exactly like the engine."""

    def __init__(self, value=0) -> None:
        self.min, self.max, self.step = 0, 40, 1
        self.mandatory = 1
        self._v = value

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = float(v)  # "7F" raises ValueError, as HonParameterRange would


def _hw_commands(*, mandatory: int = 1, pinned: bool = True) -> dict:
    params = {name: FixedParam("00:00", mandatory=mandatory) for name in _SLOTS + _UNEXPOSED}
    params["opp1EcoDays"] = MaskParam()
    if pinned:
        params["operationName"] = FixedParam("grSetVacDate")
    return {"settings": RecordingCommand(params)}


def _attrs(**overrides) -> dict:
    attrs = {name: "00:00" for name in _SLOTS + _UNEXPOSED}
    attrs["opp1EcoDays"] = "7F"
    attrs.update(overrides)
    return attrs


def _setup(*, app_type: str = "HW", commands=None, attributes=None):
    from custom_components.addhon import time as time_platform
    from custom_components.addhon.const import DOMAIN

    commands = _hw_commands() if commands is None else commands
    attributes = _attrs() if attributes is None else attributes
    coordinator = FakeCoordinator(
        {
            APPLIANCE_ID: {
                "type": app_type,
                "name": "Boiler",
                "attributes": attributes,
                "appliance": FakeAppliance(commands, attributes),
            }
        }
    )
    hass = FakeHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
    )
    added: list = []
    asyncio.run(time_platform.async_setup_entry(hass, FakeEntry(), added.extend))
    for entity in added:
        entity.hass = hass
    return added, commands


def _by_key(entities):
    return {e.entity_description.key: e for e in entities}


class GatingTest(unittest.TestCase):
    def test_exactly_the_heating_pair_exists(self) -> None:
        # The appliance exposes nine slots; ONE window is a product decision, and this
        # pins it (the rest stays reachable via addhon.send_command).
        entities = _by_key(_setup()[0])
        self.assertEqual(
            set(entities), {"heating_window_start", "heating_window_end"}
        )

    def test_the_heating_pair_is_on_by_default(self) -> None:
        entities = _by_key(_setup()[0])
        self.assertTrue(
            all(e._attr_entity_registry_enabled_default for e in entities.values())
        )

    def test_known_operation_params_pass_the_gate_regardless_of_mandatory(self) -> None:
        # Since v5.30 the gate keys on the known-operation table (grSetEcoTime), not on
        # the mandatory flag -- the flag only ever predicted the shadow echo.
        entities, _ = _setup(commands=_hw_commands(mandatory=0))
        self.assertEqual(len(entities), len(_SLOTS))

    def test_the_write_names_the_executing_operation(self) -> None:
        # The one line the whole saga comes down to: the payload must say
        # operationName=grSetEcoTime, or the appliance discards the field after the
        # shadow echo (differentially proven by the probe, 2026-08-26).
        import datetime as dt

        entities, commands = _setup()
        start = _by_key(entities)["heating_window_start"]
        asyncio.run(start.async_set_value(dt.time(11, 0)))
        self.assertEqual(
            commands["settings"].parameters["operationName"].value, "grSetEcoTime"
        )

    def test_an_unpinned_command_gets_no_operation_name(self) -> None:
        # A free-write command has no operation envelope; naming one would send a
        # parameter the command does not carry, and the engine would refuse the write.
        import datetime as dt

        entities, commands = _setup(commands=_hw_commands(pinned=False))
        start = _by_key(entities)["heating_window_start"]
        asyncio.run(start.async_set_value(dt.time(11, 0)))
        self.assertNotIn("operationName", commands["settings"].parameters)
        self.assertEqual(commands["settings"].parameters["opp2EcoStartTime1"].value, "11:00")

    def test_an_unpinned_command_is_never_gated(self) -> None:
        entities, _ = _setup(commands=_hw_commands(mandatory=0, pinned=False))
        self.assertEqual(len(entities), len(_SLOTS))  # the heating pair

    def test_a_device_without_the_schedule_gets_nothing(self) -> None:
        entities, _ = _setup(commands={"settings": RecordingCommand({})})
        self.assertEqual(entities, [])

    def test_another_appliance_type_is_not_evaluated(self) -> None:
        entities, _ = _setup(app_type="AC")
        self.assertEqual(entities, [])


class ReadTest(unittest.TestCase):
    def test_the_state_is_the_appliance_reading(self) -> None:
        entities = _by_key(
            _setup(attributes=_attrs(opp2EcoStartTime1="11:00", opp2EcoEndTime1="16:00"))[0]
        )
        self.assertEqual(entities["heating_window_start"].native_value, datetime.time(11, 0))
        self.assertEqual(entities["heating_window_end"].native_value, datetime.time(16, 0))

    def test_an_unset_slot_reads_midnight(self) -> None:
        # Unlike the holiday dates, 00:00 is a real reading here and a time entity has no
        # empty state to fall back on.
        entities = _by_key(_setup()[0])
        self.assertEqual(entities["heating_window_start"].native_value, datetime.time(0, 0))

    def test_an_unusable_reading_is_unknown(self) -> None:
        entities = _by_key(_setup(attributes=_attrs(opp2EcoStartTime1="0"))[0])
        self.assertIsNone(entities["heating_window_start"].native_value)


class WriteTest(unittest.TestCase):
    def test_it_sends_hh_mm_and_refreshes(self) -> None:
        entities, commands = _setup()
        start = _by_key(entities)["heating_window_start"]
        asyncio.run(start.async_set_value(datetime.time(11, 0)))
        self.assertEqual(commands["settings"].parameters["opp2EcoStartTime1"].value, "11:00")
        self.assertEqual(commands["settings"].send_calls, 1)

    def test_seconds_are_dropped(self) -> None:
        # The appliance reports minute resolution; "11:30:00" would never match back and
        # the entity would look like the write failed.
        entities, commands = _setup()
        end = _by_key(entities)["heating_window_end"]
        asyncio.run(end.async_set_value(datetime.time(16, 30, 45)))
        self.assertEqual(commands["settings"].parameters["opp2EcoEndTime1"].value, "16:30")

    def test_the_group_1_mask_is_preserved_not_managed(self) -> None:
        # opp1EcoDays belongs to the OTHER subsystem (off-peak tariff). The window write
        # no longer manages it; the mistyped-parameter preservation still carries the
        # appliance's own reading through, because the range[0,40] parameter cannot hold
        # "7F" and the command would otherwise send its schema default over it.
        entities, commands = _setup(attributes=_attrs(opp1EcoDays="7F"))
        start = _by_key(entities)["heating_window_start"]
        asyncio.run(start.async_set_value(datetime.time(11, 0)))
        self.assertEqual(commands["settings"].sent["opp1EcoDays"], "7F")



    def test_it_writes_only_its_own_slot(self) -> None:
        entities, commands = _setup()
        asyncio.run(
            _by_key(entities)["heating_window_start"].async_set_value(datetime.time(23, 0))
        )
        params = commands["settings"].parameters
        self.assertEqual(params["opp2EcoStartTime1"].value, "23:00")
        self.assertEqual(params["opp2EcoEndTime1"].value, "00:00")
        self.assertEqual(params["opp1EcoStartTime1"].value, "00:00")

    def test_without_a_client_it_refuses(self) -> None:
        from custom_components.addhon import time as time_platform
        from custom_components.addhon.const import DOMAIN
        from homeassistant.exceptions import HomeAssistantError

        commands = _hw_commands()
        coordinator = FakeCoordinator(
            {
                APPLIANCE_ID: {
                    "type": "HW",
                    "name": "Boiler",
                    "attributes": _attrs(),
                    "appliance": FakeAppliance(commands),
                }
            }
        )
        # No client in the entry: the READ half stays alive, the write refuses.
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        asyncio.run(time_platform.async_setup_entry(hass, FakeEntry(), added.extend))
        entity = _by_key(added)["heating_window_start"]
        entity.hass = hass
        with self.assertRaises(HomeAssistantError):
            asyncio.run(entity.async_set_value(datetime.time(11, 0)))
        self.assertEqual(commands["settings"].send_calls, 0)


if __name__ == "__main__":
    unittest.main()
