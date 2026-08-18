# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the date platform (the water heater's scheduled vacation window).

Modeled on the REAL HP250M7C-F9 diagnostics dump (2026-08, vacation set in the app
for 2026-08-18 -> 2026-08-22): the `settings` command carries vacStartDate /
vacEndDate as typology-"fixed" ISO dates next to operationName="grSetVacDate", and
the cloud shadow reports the same two values as plain attributes.

Verifies:
- capability-gating: entities exist only when the device exposes the parameters in
  a write command, and only for the water-heater types (HW/WH);
- the shadow ISO dates parse into native_value (absent/garbage -> None, never a guess);
- a write sends the ISO string through the settings command (the full parameters
  group, so operationName and the untouched sibling ride along) and refreshes;
- an inverted window (start after end, either half) is refused up front with the
  translated error and NOTHING is sent; an absent sibling skips the guard.

Stdlib unittest with inline Home Assistant stubs (no HA install required). The stubs
are getattr-guarded so they coexist with the other test modules in the pytest process.
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
    date_mod = _mod("homeassistant.components.date")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class DateEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        entity_category: object | None = None

    date_mod.DateEntityDescription = getattr(date_mod, "DateEntityDescription", DateEntityDescription)
    date_mod.DateEntity = getattr(date_mod, "DateEntity", type("DateEntity", (), {}))

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
    components.date = date_mod


_install_homeassistant_stubs()


class FixedParam:
    """Mimics HonParameterFixed: a settable value with NO validation."""

    def __init__(self, value) -> None:
        self.value = value


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


APPLIANCE_ID = "hw-1"


def _hw_commands(*, with_dates: bool = True) -> dict:
    """HP250M7C-F9-shaped settings command: the vacation window next to the
    operation marker and an ordinary parameter, all in the same parameters group."""
    params = {
        "onOffStatus": FixedParam(1),
        "operationName": FixedParam("grSetVacDate"),
    }
    if with_dates:
        params["vacStartDate"] = FixedParam("2026-08-18")
        params["vacEndDate"] = FixedParam("2026-08-22")
    return {"settings": RecordingCommand(params)}


def _attrs(**overrides) -> dict:
    attrs = {"vacStartDate": "2026-08-18", "vacEndDate": "2026-08-22"}
    attrs.update(overrides)
    return attrs


def _setup(
    *,
    app_type: str = "HW",
    commands: dict | None = None,
    attributes: dict | None = None,
):
    """Run async_setup_entry against one fake appliance; return (entities, commands)."""
    from custom_components.addhon import date as date_platform
    from custom_components.addhon.const import DOMAIN

    commands = _hw_commands() if commands is None else commands
    coordinator = FakeCoordinator(
        {
            APPLIANCE_ID: {
                "type": app_type,
                "name": "Boiler",
                "appliance": FakeAppliance(commands),
                "attributes": _attrs() if attributes is None else attributes,
            }
        }
    )
    entry = FakeEntry()
    hass = FakeHass(
        {DOMAIN: {entry.entry_id: {"coordinator": coordinator, "client": FakeClient()}}}
    )
    coordinator.hass = hass
    added: list = []
    asyncio.run(
        date_platform.async_setup_entry(hass, entry, lambda new: added.extend(new))
    )
    for entity in added:
        entity.hass = hass
    return added, commands


def _entity(added, key: str):
    return next(e for e in added if e.entity_description.key == key)


class GatingTest(unittest.TestCase):
    def test_hw_with_both_params_gets_both_entities(self) -> None:
        added, _ = _setup()
        self.assertEqual(
            {e.entity_description.key for e in added},
            {"vacation_start_date", "vacation_end_date"},
        )

    def test_no_params_no_entities(self) -> None:
        added, _ = _setup(commands=_hw_commands(with_dates=False))
        self.assertEqual(added, [])

    def test_type_outside_the_water_heater_family_is_not_evaluated(self) -> None:
        # Same schema, but an AC: the registry gates by type before the capability gate.
        added, _ = _setup(app_type="AC")
        self.assertEqual(added, [])

    def test_identity_and_category(self) -> None:
        added, _ = _setup()
        start = _entity(added, "vacation_start_date")
        self.assertEqual(start._attr_unique_id, f"{APPLIANCE_ID}_vacation_start_date")
        self.assertEqual(start._attr_translation_key, "vacation_start_date")
        self.assertEqual(str(start.entity_description.entity_category), "config")


class ReadTest(unittest.TestCase):
    def test_shadow_iso_dates_parse(self) -> None:
        added, _ = _setup()
        self.assertEqual(
            _entity(added, "vacation_start_date").native_value,
            datetime.date(2026, 8, 18),
        )
        self.assertEqual(
            _entity(added, "vacation_end_date").native_value,
            datetime.date(2026, 8, 22),
        )

    def test_absent_attribute_reads_none(self) -> None:
        added, _ = _setup(attributes={})
        self.assertIsNone(_entity(added, "vacation_start_date").native_value)

    def test_garbage_reads_none(self) -> None:
        added, _ = _setup(attributes=_attrs(vacStartDate="--"))
        self.assertIsNone(_entity(added, "vacation_start_date").native_value)


class WriteTest(unittest.TestCase):
    def test_set_start_sends_iso_through_settings_and_refreshes(self) -> None:
        added, commands = _setup()
        start = _entity(added, "vacation_start_date")
        asyncio.run(start.async_set_value(datetime.date(2026, 8, 19)))
        command = commands["settings"]
        self.assertEqual(command.send_calls, 1)
        self.assertEqual(command.sent["vacStartDate"], "2026-08-19")
        # The whole parameters group goes out: the operation marker and the
        # untouched sibling ride along (the write model the real device proved).
        self.assertEqual(command.sent["operationName"], "grSetVacDate")
        self.assertEqual(command.sent["vacEndDate"], "2026-08-22")
        self.assertEqual(start.coordinator.refreshes, 1)

    def test_set_end_sends_iso(self) -> None:
        added, commands = _setup()
        end = _entity(added, "vacation_end_date")
        asyncio.run(end.async_set_value(datetime.date(2026, 8, 25)))
        self.assertEqual(commands["settings"].sent["vacEndDate"], "2026-08-25")

    def test_single_day_window_is_allowed(self) -> None:
        added, commands = _setup()
        start = _entity(added, "vacation_start_date")
        asyncio.run(start.async_set_value(datetime.date(2026, 8, 22)))
        self.assertEqual(commands["settings"].send_calls, 1)


class OrderingGuardTest(unittest.TestCase):
    def _assert_refused(self, entity, value, commands) -> None:
        from homeassistant.exceptions import HomeAssistantError

        with self.assertRaises(HomeAssistantError) as ctx:
            asyncio.run(entity.async_set_value(value))
        self.assertEqual(
            getattr(ctx.exception, "translation_key", None), "vacation_dates_inverted"
        )
        placeholders = getattr(ctx.exception, "translation_placeholders", None) or {}
        self.assertEqual(set(placeholders), {"start", "end"})
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_start_after_end_is_refused_before_sending(self) -> None:
        added, commands = _setup()
        self._assert_refused(
            _entity(added, "vacation_start_date"), datetime.date(2026, 8, 23), commands
        )

    def test_end_before_start_is_refused_before_sending(self) -> None:
        added, commands = _setup()
        self._assert_refused(
            _entity(added, "vacation_end_date"), datetime.date(2026, 8, 17), commands
        )

    def test_absent_sibling_skips_the_guard(self) -> None:
        # The shadow reports no end date: there is no window to invert, so the
        # write must go through rather than dead-end the control.
        added, commands = _setup(attributes={"vacStartDate": "2026-08-18"})
        start = _entity(added, "vacation_start_date")
        asyncio.run(start.async_set_value(datetime.date(2026, 8, 30)))
        self.assertEqual(commands["settings"].send_calls, 1)


if __name__ == "__main__":
    unittest.main()
