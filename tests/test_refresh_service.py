# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the domain-wide ``addhon.refresh`` service.

The service forces an immediate cloud poll on every loaded config entry: the
automation-callable equivalent of the per-device "Refresh now" button (asked for
in discussion #34). It is global to the domain (registered once, removed on the
last unload), takes no fields and no target, isolates per-entry failures and must
never re-raise to the caller.

Like test_debug_panel, this uses stdlib unittest with hand-rolled HA stubs so no
real Home Assistant is required. ``FakeHass`` here gains a minimal services
registry (the debug-panel one has none): it records the registered handlers keyed
by ``(domain, name)`` and supports ``has_service`` / ``async_register`` /
``async_remove`` -- exactly what ``_async_register_services`` and
``async_unload_entry`` touch. A source-level wiring guard mirrors
test_mqtt_log_level.
"""
from __future__ import annotations

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


def _install_stubs() -> None:
    """Minimal HA stubs needed to import custom_components.addhon (the package).

    Tolerant ``getattr`` defaults mean the first test module that wins the shared
    ``sys.modules`` race keeps its richer stub; we only fill the gaps so importing
    ``__init__`` works under this module in isolation too.
    """
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    if not hasattr(core, "callback"):
        core.callback = lambda func: func

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    helpers = _mod("homeassistant.helpers")
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(
        uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = uc

    # voluptuous is imported lazily inside _async_register_services (to build the
    # level schema of the OTHER two services). It is not a test dependency, so
    # stub it when absent -- the refresh service itself takes no schema.
    vol = sys.modules.get("voluptuous")
    if vol is None or not hasattr(vol, "Marker"):
        vol = _mod("voluptuous")
        vol.Schema = lambda schema=None, **kwargs: schema

        class _Marker:
            def __init__(self, key, *args, **kwargs):
                self.key = key
                self.default = kwargs.get("default")

        vol.Required = _Marker
        vol.Optional = _Marker
        vol.In = lambda container=None, *args, **kwargs: container


_install_stubs()

from custom_components.addhon import _async_register_services  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    DOMAIN,
    SERVICE_REFRESH,
    SERVICE_SET_LOG_LEVEL,
    SERVICE_PROBE_SETTINGS_OPERATION,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_MQTT_LOG_LEVEL,
)

COMPONENT = REPO_ROOT / "custom_components" / "addhon"
INIT = COMPONENT / "__init__.py"
CONST = COMPONENT / "const.py"
SERVICES = COMPONENT / "services.yaml"


class FakeServices:
    """Minimal HA services registry: records handlers keyed by (domain, name)."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], object] = {}

    def has_service(self, domain: str, name: str) -> bool:
        return (domain, name) in self.handlers

    def async_register(self, domain, name, handler, schema=None) -> None:
        self.handlers[(domain, name)] = handler

    def async_remove(self, domain, name) -> None:
        self.handlers.pop((domain, name), None)


class FakeHass:
    def __init__(self, data=None) -> None:
        self.data = data or {}
        self.services = FakeServices()


class FakeCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class RaisingCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1
        raise RuntimeError("boom")


class SyncRaisingCoordinator:
    """async_request_refresh raises SYNCHRONOUSLY (not async) when called -- i.e.
    before yielding a coroutine. The handler wraps the call inside a coroutine so even
    this is captured by gather(return_exceptions=True) rather than escaping to the
    caller and aborting the other refreshes."""

    def async_request_refresh(self):
        raise RuntimeError("sync boom")


class FakeServiceCall:
    """A trivial ServiceCall: the refresh handler ignores ``data`` entirely."""

    def __init__(self) -> None:
        self.data: dict = {}


def _entry_data(coordinator, entry_id: str) -> dict:
    return {
        entry_id: {
            "coordinator": coordinator,
            "client": None,
            "integration_version": "9.9.9",
        }
    }


class SendCommandServiceTest(unittest.IsolatedAsyncioTestCase):
    """The diagnostic write service (v5.24.0).

    It exists because some appliances expose a `settings` command that performs ONE
    operation named by a fixed `operationName`, and the cloud only ever advertises the
    operation it is pinned to. Five captures of a real HP250M7C-F9 showed the others
    appear in no schema, no attribute and no command history (which records program
    starts only), so they can be tried and nothing else. This service is therefore
    DELIBERATELY ungated -- the one place in the integration that is.
    """

    class _Registry:
        def __init__(self, identifiers):
            self._identifiers = identifiers

        def async_get(self, device_id):
            if device_id not in self._identifiers:
                return None
            return type("Device", (), {"identifiers": self._identifiers[device_id]})()

    class _Appliance:
        def __init__(self):
            self.commands = {}

    def _hass(self, appliance, coordinator=None):
        coordinator = coordinator or FakeCoordinator()
        coordinator.data = {"app-1": {"appliance": appliance, "type": "HW"}}
        hass = FakeHass({DOMAIN: {"e1": {"coordinator": coordinator, "client": object()}}})
        _async_register_services(hass)
        return hass, coordinator

    def _call(self, **data):
        call = FakeServiceCall()
        call.data = data
        return call

    def _patch(self, hass, sent):
        """Stub the two function-local imports the handler makes."""
        import sys
        import types

        dr = types.ModuleType("homeassistant.helpers.device_registry")
        dr.async_get = lambda _hass: self._Registry(
            {"dev-1": {(DOMAIN, "app-1")}, "dev-other": {("other", "x")}}
        )
        helpers = sys.modules["homeassistant.helpers"]
        previous = getattr(helpers, "device_registry", None)
        helpers.device_registry = dr
        sys.modules["homeassistant.helpers.device_registry"] = dr

        import importlib

        hon_commands = importlib.import_module("custom_components.addhon.hon_commands")

        async def _send(_hass, _client, appliance, command_name, params):
            sent.append((command_name, dict(params)))

        original = hon_commands.async_send_command
        hon_commands.async_send_command = _send
        return lambda: (
            setattr(helpers, "device_registry", previous),
            setattr(hon_commands, "async_send_command", original),
        )

    async def test_it_sends_the_raw_parameters_and_refreshes(self) -> None:
        hass, coordinator = self._hass(self._Appliance())
        sent: list = []
        undo = self._patch(hass, sent)
        try:
            await hass.services.handlers[(DOMAIN, SERVICE_SEND_COMMAND)](
                self._call(
                    device_id=["dev-1"],
                    command="settings",
                    parameters={"operationName": "grSetVacDate", "vacStartDate": "2026-09-01"},
                )
            )
        finally:
            undo()
        self.assertEqual(
            sent,
            [("settings", {"operationName": "grSetVacDate", "vacStartDate": "2026-09-01"})],
        )
        self.assertEqual(coordinator.refreshes, 1)

    async def test_values_are_sent_as_strings(self) -> None:
        # The engine's str_to_float truncates a float silently; every write path in this
        # integration sends strings for that reason, and a service call carrying a YAML
        # number must not be the exception.
        hass, _ = self._hass(self._Appliance())
        sent: list = []
        undo = self._patch(hass, sent)
        try:
            await hass.services.handlers[(DOMAIN, SERVICE_SEND_COMMAND)](
                self._call(device_id="dev-1", command="settings", parameters={"tempSel": 63})
            )
        finally:
            undo()
        self.assertEqual(sent, [("settings", {"tempSel": "63"})])

    async def test_an_empty_parameter_set_is_refused(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        hass, _ = self._hass(self._Appliance())
        sent: list = []
        undo = self._patch(hass, sent)
        try:
            with self.assertRaises(HomeAssistantError) as ctx:
                await hass.services.handlers[(DOMAIN, SERVICE_SEND_COMMAND)](
                    self._call(device_id="dev-1", command="settings", parameters={})
                )
        finally:
            undo()
        self.assertEqual(ctx.exception.translation_key, "send_command_no_parameters")
        self.assertEqual(sent, [])

    async def test_a_foreign_device_is_refused(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        hass, _ = self._hass(self._Appliance())
        sent: list = []
        undo = self._patch(hass, sent)
        try:
            with self.assertRaises(HomeAssistantError) as ctx:
                await hass.services.handlers[(DOMAIN, SERVICE_SEND_COMMAND)](
                    self._call(
                        device_id=["dev-other"], command="settings", parameters={"a": "1"}
                    )
                )
        finally:
            undo()
        self.assertEqual(ctx.exception.translation_key, "send_command_unknown_device")
        self.assertEqual(sent, [])


class OperationCandidateTest(unittest.TestCase):
    """The ladder of operation names the probe tries, derived from the parameter."""

    @staticmethod
    def _candidates(parameter):
        import importlib

        module = importlib.import_module("custom_components.addhon.hon_commands")
        return module.settings_operation_candidates(parameter)

    def test_it_derives_the_one_confirmed_name(self) -> None:
        # grSetVacDate is the operation the HP250M7C-F9 pins its settings command to, and
        # the only name ever confirmed. The first-and-last-word rung is what produces it.
        self.assertIn("grSetVacDate", self._candidates("vacStartDate"))
        self.assertIn("grSetVacDate", self._candidates("vacEndDate"))

    def test_the_whole_name_comes_first(self) -> None:
        self.assertEqual(
            self._candidates("sterilizationTime")[0], "grSetSterilizationTime"
        )

    def test_prefixes_follow_longest_first(self) -> None:
        self.assertEqual(
            self._candidates("timingPowerOn"),
            ["grSetTimingPowerOn", "grSetTimingOn", "grSetTimingPower", "grSetTiming"],
        )

    def test_there_are_no_duplicates(self) -> None:
        for parameter in ("vacStartDate", "sterilizationTime", "onOffStatus"):
            with self.subTest(parameter=parameter):
                candidates = self._candidates(parameter)
                self.assertEqual(len(candidates), len(set(candidates)))

    def test_an_empty_name_yields_nothing(self) -> None:
        self.assertEqual(self._candidates(""), [])


class ProbeSettingsOperationTest(unittest.IsolatedAsyncioTestCase):
    """The probe: send a candidate, wait, read the parameter back, stop on the first hit."""

    WINNER = "grSetSterilization"

    class _Registry:
        def async_get(self, device_id):
            if device_id != "dev-1":
                return None
            return type("Device", (), {"identifiers": {(DOMAIN, "app-1")}})()

    def _hass(self, initial="22:00"):
        coordinator = FakeCoordinator()
        coordinator.data = {
            "app-1": {
                "appliance": object(),
                "type": "HW",
                "attributes": {"sterilizationTime": initial},
            }
        }

        async def _refresh():
            coordinator.refreshes += 1

        coordinator.async_refresh = _refresh
        hass = FakeHass({DOMAIN: {"e1": {"coordinator": coordinator, "client": object()}}})
        _async_register_services(hass)
        return hass, coordinator

    def _patch(self, coordinator, tried, *, winner=WINNER):
        import importlib
        import sys
        import types

        dr = types.ModuleType("homeassistant.helpers.device_registry")
        dr.async_get = lambda _hass: self._Registry()
        helpers = sys.modules["homeassistant.helpers"]
        previous_dr = getattr(helpers, "device_registry", None)
        helpers.device_registry = dr
        sys.modules["homeassistant.helpers.device_registry"] = dr

        components = sys.modules.setdefault(
            "homeassistant.components", types.ModuleType("homeassistant.components")
        )
        pn = types.ModuleType("homeassistant.components.persistent_notification")
        self.notifications = []
        pn.async_create = lambda _hass, message, title=None, notification_id=None: (
            self.notifications.append(message)
        )
        previous_pn = getattr(components, "persistent_notification", None)
        components.persistent_notification = pn
        sys.modules["homeassistant.components.persistent_notification"] = pn

        hon_commands = importlib.import_module("custom_components.addhon.hon_commands")
        original = hon_commands.async_send_command

        async def _send(_hass, _client, _appliance, _command, params):
            tried.append(params["operationName"])
            if params["operationName"] == winner:
                coordinator.data["app-1"]["attributes"]["sterilizationTime"] = params[
                    "sterilizationTime"
                ]

        hon_commands.async_send_command = _send
        return lambda: (
            setattr(helpers, "device_registry", previous_dr),
            setattr(components, "persistent_notification", previous_pn),
            setattr(hon_commands, "async_send_command", original),
        )

    def _call(self, **data):
        call = FakeServiceCall()
        call.data = {"command": "settings", "settle": 0, **data}
        return call

    async def _run(self, hass, tried, **data):
        payload = {
            "device_id": "dev-1",
            "parameter": "sterilizationTime",
            "value": "07:00",
        }
        payload.update(data)
        await hass.services.handlers[(DOMAIN, SERVICE_PROBE_SETTINGS_OPERATION)](
            self._call(**payload)
        )

    async def test_it_stops_at_the_first_candidate_that_works(self) -> None:
        hass, coordinator = self._hass()
        tried: list = []
        undo = self._patch(coordinator, tried)
        try:
            await self._run(hass, tried)
        finally:
            undo()
        # The whole name is tried first and misses; the shorter one wins and ends it.
        self.assertEqual(tried, ["grSetSterilizationTime", self.WINNER])
        self.assertEqual(
            coordinator.data["app-1"]["attributes"]["sterilizationTime"], "07:00"
        )
        self.assertIn(self.WINNER, self.notifications[0])

    async def test_an_explicit_candidate_list_wins_over_the_derived_one(self) -> None:
        hass, coordinator = self._hass()
        tried: list = []
        undo = self._patch(coordinator, tried, winner="grSetWhatever")
        try:
            await self._run(hass, tried, operations=["grSetNope", "grSetWhatever"])
        finally:
            undo()
        self.assertEqual(tried, ["grSetNope", "grSetWhatever"])

    async def test_every_candidate_missing_is_reported_not_hidden(self) -> None:
        hass, coordinator = self._hass()
        tried: list = []
        undo = self._patch(coordinator, tried, winner="grSetNothingMatches")
        try:
            await self._run(hass, tried)
        finally:
            undo()
        self.assertEqual(tried, ["grSetSterilizationTime", "grSetSterilization"])
        self.assertIn("None of the", self.notifications[0])

    async def test_an_unreported_parameter_is_refused_before_writing(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        hass, coordinator = self._hass()
        tried: list = []
        undo = self._patch(coordinator, tried)
        try:
            with self.assertRaises(HomeAssistantError) as ctx:
                await self._run(hass, tried, parameter="notReported")
        finally:
            undo()
        self.assertEqual(ctx.exception.translation_key, "probe_parameter_not_reported")
        self.assertEqual(tried, [])

    async def test_a_value_already_in_place_is_refused(self) -> None:
        # Otherwise the FIRST candidate reads as a hit and the answer is meaningless.
        from homeassistant.exceptions import HomeAssistantError

        hass, coordinator = self._hass(initial="07:00")
        tried: list = []
        undo = self._patch(coordinator, tried)
        try:
            with self.assertRaises(HomeAssistantError) as ctx:
                await self._run(hass, tried)
        finally:
            undo()
        self.assertEqual(ctx.exception.translation_key, "probe_value_already_set")
        self.assertEqual(tried, [])


class RefreshServiceRegistrationTest(unittest.TestCase):
    def test_register_adds_refresh_service_without_schema(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_REFRESH))
        # All three domain-wide services land in the same registry.
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_SET_LOG_LEVEL))
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL))

    def test_registration_is_idempotent(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        first = hass.services.handlers[(DOMAIN, SERVICE_REFRESH)]
        # A second call (e.g. a second entry's setup) must not re-register / replace.
        _async_register_services(hass)
        self.assertIs(first, hass.services.handlers[(DOMAIN, SERVICE_REFRESH)])

    def test_registers_refresh_when_only_it_is_missing(self) -> None:
        # The combined early-return guard must still register refresh if the other
        # two already exist (e.g. an upgrade from a build that lacked refresh).
        hass = FakeHass()
        hass.services.handlers[(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL)] = object()
        hass.services.handlers[(DOMAIN, SERVICE_SET_LOG_LEVEL)] = object()
        _async_register_services(hass)
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_REFRESH))


class RefreshServiceBehaviorTest(unittest.IsolatedAsyncioTestCase):
    def _registered_handler(self, hass: FakeHass):
        return hass.services.handlers[(DOMAIN, SERVICE_REFRESH)]

    async def test_refreshes_every_loaded_coordinator(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        coord_a, coord_b = FakeCoordinator(), FakeCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(coord_a, "entry-a"))
        hass.data[DOMAIN].update(_entry_data(coord_b, "entry-b"))

        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, coord_a.refreshes)
        self.assertEqual(1, coord_b.refreshes)

    async def test_per_entry_failure_is_isolated(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        good, bad = FakeCoordinator(), RaisingCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(bad, "entry-bad"))
        hass.data[DOMAIN].update(_entry_data(good, "entry-good"))

        # Must NOT raise even though one coordinator blows up.
        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, bad.refreshes)
        self.assertEqual(1, good.refreshes)

    async def test_synchronous_raise_is_isolated(self) -> None:
        # A coordinator whose async_request_refresh raises SYNCHRONOUSLY (before
        # returning a coroutine) must not abort the others nor reach the caller: the
        # handler wraps each call in a coroutine so gather(return_exceptions=True)
        # captures it.
        hass = FakeHass()
        _async_register_services(hass)
        good, bad = FakeCoordinator(), SyncRaisingCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(bad, "entry-bad"))
        hass.data[DOMAIN].update(_entry_data(good, "entry-good"))

        # Must NOT raise even though one coordinator raises synchronously.
        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, good.refreshes)

    async def test_skips_none_coordinators_and_no_data(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        handler = self._registered_handler(hass)

        # No DOMAIN bucket at all: a no-op, no raise.
        await handler(FakeServiceCall())

        # An entry mid-setup may have a None coordinator; it must be skipped.
        good = FakeCoordinator()
        hass.data[DOMAIN] = {
            "entry-partial": {"coordinator": None, "client": None},
            "entry-good": {"coordinator": good, "client": None},
        }
        await handler(FakeServiceCall())
        self.assertEqual(1, good.refreshes)

    async def test_reads_hass_data_live_at_call_time(self) -> None:
        # The handler captures ``hass`` but must enumerate coordinators at call
        # time, so an entry added AFTER registration is still refreshed.
        hass = FakeHass()
        _async_register_services(hass)
        handler = self._registered_handler(hass)

        late = FakeCoordinator()
        hass.data[DOMAIN] = _entry_data(late, "entry-late")
        await handler(FakeServiceCall())
        self.assertEqual(1, late.refreshes)


class RefreshServiceWiringTest(unittest.TestCase):
    """Source-level guards: the service must stay declared and wired."""

    def test_const_declares_service_name(self) -> None:
        self.assertIn(
            'SERVICE_REFRESH = "refresh"',
            CONST.read_text(encoding="utf-8"),
        )

    def test_services_yaml_declares_refresh(self) -> None:
        self.assertIn("refresh:", SERVICES.read_text(encoding="utf-8"))

    def test_init_registers_and_unregisters_refresh(self) -> None:
        src = INIT.read_text(encoding="utf-8")
        self.assertIn("SERVICE_REFRESH", src)
        self.assertIn("async_request_refresh", src)
        # Debounced refresh like the button, not a forced async_refresh.
        self.assertNotIn("async_refresh(", src)
        # Per-entry isolation: gather with return_exceptions, never re-raise.
        self.assertIn("return_exceptions=True", src)


if __name__ == "__main__":
    unittest.main()
