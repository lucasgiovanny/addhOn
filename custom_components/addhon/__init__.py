# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import logging
from datetime import timedelta
from typing import NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)

try:
    # In real Home Assistant these symbols always exist. The import is tolerant
    # only for the test harness, which stubs homeassistant.core with the bare
    # minimum (shared sys.modules: the first stub wins, so it is more robust to
    # degrade here than to extend every stub).
    from homeassistant.core import ServiceCall, callback
except ImportError:  # pragma: no cover - only under the test stub
    ServiceCall = object  # type: ignore[assignment,misc]

    def callback(func):  # type: ignore[no-redef]
        return func
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    APPLIANCE_HW,
    APPLIANCE_TD,
    APPLIANCE_WH,
    ATTR_LEVEL,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL,
    ATTR_COMMAND,
    ATTR_OPERATIONS,
    ATTR_PARAMETER,
    ATTR_PARAMETERS,
    ATTR_SETTLE,
    ATTR_VALUE,
    PROBE_SETTLE_DEFAULT,
    SERVICE_PROBE_SETTINGS_OPERATION,
    SERVICE_REFRESH,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_LOG_LEVEL,
    SERVICE_SET_MQTT_LOG_LEVEL,
)
from .logging_utils import (
    MQTT_LOG_LEVELS,
    apply_integration_log_level,
    apply_mqtt_log_level,
    reset_integration_log_level,
    silence_mqtt_noise,
)
from .debug_utils import redact_id, redact_mac
from . import program_labels

_LOGGER = logging.getLogger(__name__)


@callback
def _target_appliances(hass: HomeAssistant, registry, device_ids):
    """Yield (entry_data, appliance_id, appliance, coordinator) for the targeted devices.

    Shared by the two diagnostic write services. A device that is not one of this
    integration's appliances is an ERROR rather than a silent no-op: a service that
    quietly did nothing is exactly the failure mode these services exist to diagnose.
    """
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    for device_id in device_ids or []:
        device = registry.async_get(device_id)
        appliance_ids = {
            identifier
            for domain, identifier in (device.identifiers if device else set())
            if domain == DOMAIN
        }
        found = False
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict):
                continue
            coordinator = entry_data.get("coordinator")
            data = getattr(coordinator, "data", None)
            if not isinstance(data, dict):
                continue
            for appliance_id in appliance_ids & set(data):
                found = True
                yield (
                    entry_data,
                    appliance_id,
                    data[appliance_id].get("appliance"),
                    coordinator,
                )
        if not found:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_command_unknown_device",
                translation_placeholders={"device": device_id},
            )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register (only once) the service for the MQTT log level.

    On the first registration it also applies the default silencing of the
    realtime MQTT noise. The service is global to the domain, not per-entry, so it
    is idempotent: if already present it does nothing.

    voluptuous is imported here (not at module level) so the import of __init__
    does not depend on voluptuous: the test harness imports the package without
    always providing its stub, while this function only runs in real HA.
    """
    mqtt_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL)
    log_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_LOG_LEVEL)
    refresh_service_exists = hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    send_service_exists = hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND)
    probe_service_exists = hass.services.has_service(
        DOMAIN, SERVICE_PROBE_SETTINGS_OPERATION
    )
    if (
        mqtt_service_exists
        and log_service_exists
        and refresh_service_exists
        and send_service_exists
        and probe_service_exists
    ):
        return

    import voluptuous as vol

    # First registration (HA start/restart): silence the noise by default.
    # On a reload of a single entry the service stays registered, so a debug level
    # possibly set at runtime is not re-silenced.
    if not mqtt_service_exists:
        silence_mqtt_noise()

    async def _handle_set_mqtt_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_mqtt_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "realtime MQTT log level set to %s", level_name.upper()
        )

    async def _handle_set_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_integration_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "Haier hOn diagnostic log level set to %s", level_name.upper()
        )

    async def _handle_refresh(call: ServiceCall) -> None:
        """Force an immediate cloud poll on every loaded config entry.

        Domain-wide equivalent of the per-device "Refresh now" button: it reads
        hass.data live at call time and asks each loaded coordinator for a
        debounced refresh (async_request_refresh, like the button). Per-entry
        failures are isolated (asyncio.gather(..., return_exceptions=True)) and
        logged at warning; the service NEVER re-raises to the caller, so one
        unhealthy account does not break an automation refreshing the others.
        """
        coordinators = [
            entry_data["coordinator"]
            for entry_data in hass.data.get(DOMAIN, {}).values()
            if isinstance(entry_data, dict) and entry_data.get("coordinator") is not None
        ]
        _LOGGER.debug(
            "Refresh service: requesting refresh on %d coordinator(s)",
            len(coordinators),
        )

        async def _refresh_one(coordinator) -> None:
            # Wrap the call INSIDE a coroutine so even a SYNCHRONOUS raise from
            # async_request_refresh (e.g. a future refactor storing a wrapper /
            # wrong-typed object) is captured by gather(return_exceptions=True)
            # instead of escaping the generator and aborting the other refreshes.
            await coordinator.async_request_refresh()

        results = await asyncio.gather(
            *(_refresh_one(coordinator) for coordinator in coordinators),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.warning("Refresh service: a coordinator refresh failed: %s", result)

    async def _handle_send_command(call: ServiceCall) -> None:
        """Send ONE raw command to ONE appliance. Diagnostic, and deliberately ungated.

        Every other control in this integration is capability-gated, and the settings
        writes are additionally refused when the command is pinned to an operation that
        would swallow them (hon_commands.settings_write_blocked). This service is the
        exception, and it is the reason it exists: on an appliance whose `settings`
        command performs a single operation named by a fixed `operationName`, the cloud
        advertises ONLY the operation it is currently pinned to. The others appear in no
        schema, in no attribute and -- as five captures of a real HP250M7C-F9 showed --
        in no command history either, which records program starts only. They can be
        tried, and nothing else.

        So: pass the parameters you want AND the `operationName` you are testing. A wrong
        operation name is the same no-op the integration is trying to avoid shipping; a
        right one is a discovery, and it belongs in
        hon_commands.SETTINGS_PARAM_OPERATIONS so the affected controls become real.

        Parameter values still go through the engine's own setters, so a range or enum
        parameter rejects an out-of-schema value exactly as it would from an entity.
        """
        from homeassistant.helpers import device_registry as dr

        from .hon_commands import async_send_command

        registry = dr.async_get(hass)
        command_name = call.data[ATTR_COMMAND]
        parameters = {
            str(key): str(value)
            for key, value in (call.data.get(ATTR_PARAMETERS) or {}).items()
        }
        if not parameters:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_command_no_parameters",
            )
        for entry_data, appliance_id, appliance, coordinator in _target_appliances(
            hass, registry, call.data.get("device_id")
        ):
            _LOGGER.info(
                "send_command service: %s %s -> id=%s",
                command_name,
                sorted(parameters),
                redact_id(appliance_id),
            )
            await async_send_command(
                hass, entry_data.get("client"), appliance, command_name, parameters
            )
            if coordinator is not None:
                await coordinator.async_request_refresh()

    level_schema = vol.Schema(
        {vol.Required(ATTR_LEVEL, default="debug"): vol.In(tuple(MQTT_LOG_LEVELS))}
    )

    if not mqtt_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_MQTT_LOG_LEVEL,
            _handle_set_mqtt_log_level,
            schema=level_schema,
        )

    if not log_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_LOG_LEVEL,
            _handle_set_log_level,
            schema=level_schema,
        )

    if not refresh_service_exists:
        # No schema: the service takes no fields and no target (domain-wide).
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH,
            _handle_refresh,
        )

    async def _handle_probe_settings_operation(call: ServiceCall) -> None:
        """Try a ladder of `operationName` candidates until the appliance honours one.

        The manual version of this is `send_command`, one candidate at a time. It exists
        because the operation a settings parameter belongs to CANNOT be read: the cloud
        advertises only the operation the command is pinned to, the shadow mirrors that
        same value, and the command history records program starts only. The names have
        to be tried, and trying is exactly what this automates.

        Each round sends {operationName: candidate, parameter: value}, waits for the
        appliance to apply and the shadow to catch up, then reads the parameter back. The
        first candidate that moves it is the answer -- and belongs in
        hon_commands.SETTINGS_PARAM_OPERATIONS, which is what turns the read-only
        controls for that group into real ones.

        Refuses up front when the parameter is not mirrored in the shadow (the result
        could not be observed) or already holds the target value (every candidate would
        look like a hit).
        """
        from homeassistant.components import persistent_notification
        from homeassistant.helpers import device_registry as dr

        from .hon_commands import async_send_command, settings_operation_candidates

        registry = dr.async_get(hass)
        command_name = call.data[ATTR_COMMAND]
        parameter = call.data[ATTR_PARAMETER]
        value = str(call.data[ATTR_VALUE])
        settle = float(call.data.get(ATTR_SETTLE, PROBE_SETTLE_DEFAULT))
        candidates = [
            str(item) for item in (call.data.get(ATTR_OPERATIONS) or [])
        ] or settings_operation_candidates(parameter)

        for entry_data, appliance_id, appliance, coordinator in _target_appliances(
            hass, registry, call.data.get("device_id")
        ):
            def _reported() -> str | None:
                data = getattr(coordinator, "data", None) or {}
                attributes = (data.get(appliance_id) or {}).get("attributes") or {}
                raw = attributes.get(parameter)
                return None if raw is None else str(raw).strip()

            before = _reported()
            if before is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="probe_parameter_not_reported",
                    translation_placeholders={"parameter": parameter},
                )
            if before == value:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="probe_value_already_set",
                    translation_placeholders={"parameter": parameter, "value": value},
                )

            async def _refresh_and_read() -> str | None:
                refresh = getattr(coordinator, "async_refresh", None)
                if refresh is not None:
                    await refresh()
                return _reported()

            # ECHO-RESISTANT since v5.29.1: a MANDATORY parameter sent through the
            # pinned settings command lands in the cloud shadow and reads back as
            # applied for a minute or more before the appliance's own state publish
            # reverts it. That echo is precisely what fooled the mandatory-flag theory
            # (an opp1 window read as written for minutes and was later found
            # discarded). So a hit is only reported after it survives a SECOND wait of
            # the same length -- call with settle >= 90 when hunting, so the appliance
            # publishes in between.
            found: str | None = None
            tried: list[str] = []
            echoed: list[str] = []
            for candidate in candidates:
                tried.append(candidate)
                _LOGGER.info(
                    "probe_settings_operation: trying '%s' for %s=%s id=%s",
                    candidate,
                    parameter,
                    value,
                    redact_id(appliance_id),
                )
                await async_send_command(
                    hass,
                    entry_data.get("client"),
                    appliance,
                    command_name,
                    {"operationName": candidate, parameter: value},
                )
                await asyncio.sleep(settle)
                if await _refresh_and_read() != value:
                    continue
                await asyncio.sleep(settle)
                if await _refresh_and_read() == value:
                    found = candidate
                    break
                echoed.append(candidate)
                _LOGGER.info(
                    "probe_settings_operation: '%s' echoed and reverted", candidate
                )

            if found:
                message = (
                    f"`{parameter}` moved to `{value}` with **operationName "
                    f"`{found}`** and SURVIVED a re-check {settle:.0f}s later. Add it "
                    f"to SETTINGS_PARAM_OPERATIONS to make the matching controls "
                    f"writable."
                )
            else:
                detail = (
                    f" These echoed into the shadow and then reverted (the appliance "
                    f"discarded them): {', '.join(echoed)}."
                    if echoed
                    else ""
                )
                message = (
                    f"None of the {len(tried)} candidates moved `{parameter}` "
                    f"persistently: {', '.join(tried)}.{detail} Try other names, or "
                    f"the operation may not be reachable from the cloud at all."
                )
            _LOGGER.info("probe_settings_operation: %s", message)
            persistent_notification.async_create(
                hass,
                message,
                title="addhOn: settings operation probe",
                notification_id=f"addhon_probe_{parameter}",
            )

    if not send_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_COMMAND,
            _handle_send_command,
            # Plain voluptuous, no homeassistant.helpers.config_validation: this
            # module is imported by the test harness without HA's helper package, and
            # the handler coerces the shapes it needs anyway.
            schema=vol.Schema(
                {
                    # `object` accepts both shapes a device target arrives in (one
                    # id or a list); the handler normalises them.
                    vol.Required("device_id"): object,
                    vol.Required(ATTR_COMMAND, default="settings"): str,
                    vol.Required(ATTR_PARAMETERS): dict,
                }
            ),
        )

    if not probe_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_PROBE_SETTINGS_OPERATION,
            _handle_probe_settings_operation,
            schema=vol.Schema(
                {
                    vol.Required("device_id"): object,
                    vol.Required(ATTR_PARAMETER): str,
                    vol.Required(ATTR_VALUE): object,
                    vol.Required(ATTR_COMMAND, default="settings"): str,
                    vol.Optional(ATTR_OPERATIONS): object,
                    vol.Optional(ATTR_SETTLE, default=PROBE_SETTLE_DEFAULT): object,
                }
            ),
        )


@callback
def _apply_debug_options(entry: ConfigEntry, *, reset_when_off: bool = True) -> None:
    """Align the log levels to the two toggles persisted in entry.options.

    enable_debug=True  -> integration logger to DEBUG; False -> NOTSET
                          (they go back to inheriting the level configured in HA).
    enable_mqtt_debug=True -> realtime MQTT logger to DEBUG; False -> WARNING
                          (silenced).

    The MQTT level is applied AFTER the integration's one, so the explicit level
    of the MQTT child wins over the parent's cascade: enabling the integration's
    DEBUG does NOT turn the realtime noise back on if the MQTT toggle is off. NB
    the loggers are global to the process (see OptionsFlowHandler): with more than
    one entry (rare, multi-account) the levels are shared and changing the options
    of one entry re-applies them based on THAT entry, possibly resetting another
    one's active debug. The typical installation has a single account.

    reset_when_off=True (default, used by the options listener): an OFF toggle
    RESETS the level (NOTSET / WARNING), so disabling it from the UI takes effect
    immediately and also clears any manual override done with the set_log_level
    service. reset_when_off=False (used in async_setup_entry): an OFF toggle does
    NOT touch the loggers, so an integration DEBUG set at runtime via the services
    survives re-setups/retries (e.g. an unstable login) instead of being reset on
    every attempt; the default MQTT silencing on the first registration is still
    guaranteed by _async_register_services (which, however, on a reload of the only
    entry that removes and re-registers the services, also re-silences any MQTT
    level raised at runtime).
    """
    if entry.options.get(CONF_ENABLE_DEBUG, False):
        apply_integration_log_level(logging.DEBUG)
    elif reset_when_off:
        reset_integration_log_level()
    if entry.options.get(CONF_ENABLE_MQTT_DEBUG, False):
        apply_mqtt_log_level(logging.DEBUG)
    elif reset_when_off:
        silence_mqtt_noise()


_ENTRY_OPTS_KEY = "entry_options"


def _entry_opts(entry: ConfigEntry) -> tuple[bool, bool, bool]:
    """The options the update listener has to react to.

    (integration-debug, mqtt-debug, experimental). The first two are applied on the
    fly; the third decides which entities EXIST, so only it can require a reload.
    """
    return (
        entry.options.get(CONF_ENABLE_DEBUG, False),
        entry.options.get(CONF_ENABLE_MQTT_DEBUG, False),
        entry.options.get(CONF_ENABLE_EXPERIMENTAL, False),
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to an options change: log levels live, entity set by reload.

    A reload would tear down auth and the MQTT channel just to change a log level;
    the debug levels are therefore re-applied on the fly, as the existing services
    do. enable_experimental is different in kind: it adds or removes entities, which
    only a reload can do, so it is the one option that reloads the entry.

    HA fires update listeners on ANY entry change (data, options, title), not only
    on an options change. A data-only write -- e.g. _persist_refresh_token rotating
    the OAuth refresh token during a routine poll -- must NOT re-apply/reset the
    debug levels: that would silently kill a debug level raised at runtime via the
    set_log_level / set_mqtt_log_level service (reset_when_off=True), exactly when the
    logs are needed, and must certainly not reload. So each half acts only on the
    values it owns, and an experimental-only change leaves the loggers untouched.
    """
    current = _entry_opts(entry)
    hass_data = getattr(hass, "data", None)
    entry_data = (
        hass_data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(hass_data, dict)
        else None
    )
    previous: tuple[bool, bool, bool] | None = None
    if entry_data is not None:
        previous = entry_data.get(_ENTRY_OPTS_KEY)
        if previous == current:
            return  # entry changed but none of these options did
        # Recorded before the reload below: a reload detaches this dict from
        # hass.data, so a write afterwards would land nowhere.
        entry_data[_ENTRY_OPTS_KEY] = current
    _LOGGER.debug(
        "Options debug: options updated entry=%s enable_debug=%s enable_mqtt_debug=%s "
        "enable_experimental=%s",
        entry.entry_id,
        current[0],
        current[1],
        current[2],
    )
    # No baseline (a first call, or an entry absent from hass.data) is not evidence
    # of a change: apply the levels as before, but never reload on a guess.
    if previous is None or previous[:2] != current[:2]:
        _apply_debug_options(entry)
    if previous is not None and previous[2] != current[2]:
        _LOGGER.info(
            "Options: experimental features %s, reloading the entry",
            "enabled" if current[2] else "disabled",
        )
        await hass.config_entries.async_reload(entry.entry_id)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"


def _redact_title(title: str | None) -> str | None:
    if not title or "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


async def _async_close_client(client) -> None:
    """Close HonClient without masking the original setup/unload error."""
    try:
        await client.async_close()
    except Exception as err:
        _LOGGER.warning("Error closing HonClient: %s", err)


@callback
def _persist_refresh_token(hass: HomeAssistant, entry: ConfigEntry, hon_client) -> None:
    """Copy a rotated refresh token into entry.data, once, only on a real change.

    Single source of truth for the write rule, used by BOTH the initial setup and the
    coordinator update path, so a token rotated later (a runtime `auth.refresh()` or a
    background `_async_reauth()`) still reaches `entry.data` and survives a restart -- not
    just the first login. The non-empty AND changed guard reads `entry.data` live each
    call, so it never wipes a good token and never writes on an unchanged poll (no entry
    churn). HA-loop only (`@callback`). NEVER logs the token value."""
    new_token = hon_client.refresh_token
    stored = entry.data.get("refresh_token", "")
    if new_token and new_token != stored:
        _LOGGER.debug("Persisting rotated refresh token")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "refresh_token": new_token}
        )


def _raise_setup_error(err: Exception) -> NoReturn:
    """Classify a SETUP failure and raise the matching HA exception.

    An auth error triggers the reauth flow (ConfigEntryAuthFailed); anything else is
    ConfigEntryNotReady so HA retries setup later. Extracted from async_setup_entry so
    the branch is unit-testable (a swapped branch would otherwise pass the suite). (#11)
    """
    from .error_codes import classify, error_detail
    from .hon_client import _requires_reauth

    code = classify(err)
    # error_detail() drops a leading "ADDHON-NNN: " so the code appears ONCE. These two
    # messages are shown by Home Assistant on the config-entry page, so the user really
    # did read the code twice before (#76).
    detail = error_detail(err)
    if _requires_reauth(err):
        raise ConfigEntryAuthFailed(f"[{code.label}] Invalid hOn credentials: {detail}") from err
    raise ConfigEntryNotReady(f"[{code.label}] Unable to connect to hOn: {detail}") from err


def _raise_update_error(err: Exception) -> NoReturn:
    """Classify a COORDINATOR update failure and raise the matching HA exception.

    An auth error triggers the reauth flow (ConfigEntryAuthFailed); anything else is a
    transient UpdateFailed (the coordinator keeps its last good snapshot and retries).
    Extracted for unit-testing (#11)."""
    from .error_codes import classify, error_detail
    from .hon_client import _requires_reauth

    code = classify(err)
    detail = error_detail(err)
    if _requires_reauth(err):
        raise ConfigEntryAuthFailed(f"[{code.label}] Invalid hOn credentials: {detail}") from err
    raise UpdateFailed(f"[{code.label}] hOn update error: {detail}") from err


# "Washer-only" sensors that were mistakenly created on the tumble dryers (TD)
# too: a tumble dryer does not use water and does not report loadingPercentage
# (the app gates that statistic to WM/WD), so they stayed forever "unknown"
# entities. After the per-type refactor they are no longer created: here we clean
# up the ones already registered, ONLY on TD devices.
_TD_REMOVED_SUFFIXES = (
    "_total_water",
    "_total_energy",
    "_current_energy",
    "_current_water",
    "_loading_percentage",
)

# Water heater (HW/WH) duplicate control surfaces retired in v5.21.0: the mode
# select and the main-setpoint number both read and wrote the same parameters as
# the water_heater entity, which is now the single control. (domain, suffix)
# pairs, matched ONLY on devices of these types: the wine cooler and the oven
# keep their own legitimate '<id>_target_temp' numbers.
_HW_REMOVED = (
    ("select", "_hw_mode"),
    ("number", "_target_temp"),
    # v5.22.0: the power SWITCH. It wrote onOffStatus through the `settings` command,
    # which on this appliance is pinned to one operation and drops everything else, so
    # it never turned anything off. Power is now the water_heater entity's, written
    # through startProgram like the mode and the setpoint.
    ("switch", "_on_off"),
    # v5.27.0: the per-slot off-peak TIME entities, replaced by the single heating
    # window (one job -- "heat during my solar hours" -- one window; the other slots
    # stay readable as sensors and writable via addhon.send_command).
    ("time", "_eco_window_1_start"),
    ("time", "_eco_window_1_end"),
    ("time", "_eco_window_2_start"),
    ("time", "_eco_window_2_end"),
    ("time", "_eco_window_3_start"),
    ("time", "_eco_window_3_end"),
    # v5.28.0: the great trim. The 2026-08-26 experiments showed the schedule /
    # auxiliary-input subsystem is INACTIVE on this appliance (powerSupplySource 0 on
    # every capture; the firmware discards writes to it), so the mirrors of that
    # subsystem and the settings-backed controls whose writes it swallows were retired.
    # Only the heating window (actively pursued), the proven anti-legionella hour and
    # powerSupplySource itself (the lead) remain.
    ("time", "_silent_window_1_start"),
    ("time", "_silent_window_1_end"),
    ("time", "_silent_window_2_start"),
    ("time", "_silent_window_2_end"),
    ("sensor", "_timer_power_on"),
    ("sensor", "_timer_power_off"),
    ("sensor", "_eco_schedule_2"),
    ("sensor", "_silent_schedule"),
    ("sensor", "_eco_days"),
    ("sensor", "_sterilization_interval"),
    ("sensor", "_external_heat_source"),
    ("sensor", "_off_peak_period_scheme"),
    ("sensor", "_off_peak_heat_mode"),
    ("sensor", "_off_peak_heat_strategy"),
    ("binary_sensor", "_timer_enabled"),
    ("binary_sensor", "_silent_running"),
    ("binary_sensor", "_solar_heating"),
    ("binary_sensor", "_off_peak_input_enabled"),
    ("binary_sensor", "_off_peak_signal"),
    ("switch", "_boost"),
    ("switch", "_silent_mode"),
    ("switch", "_child_lock"),
    ("switch", "_sterilization"),
    # v5.29.0: the heating-window sensor was re-keyed (eco_schedule_1 -> heating_window)
    # when it moved from period group 1 to group 2, where the panel's schedule really
    # lives.
    ("sensor", "_eco_schedule_1"),
    ("number", "_target_temp_hc"),
    ("number", "_target_temp_pv"),
    ("number", "_target_temp_sg"),
    ("number", "_sterilization_temp"),
)


def _remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove from the registry the legacy entities no longer provided by the integration.

    - "Power" SWITCH (unique_id '<id>_power'), removed in the 2.3/2.4 refactor.
      Scoped to the switch domain on purpose: the legitimate WH `power` sensor
      (unique_id '<id>_power') and KT `current_power` sensor (unique_id
      '<id>_current_power') both end in '_power' and must NOT be purged.
    - Washer-only sensors on the tumble dryers (TD): '<td_id>_total_water',
      '_total_energy', '_current_energy', '_current_water', '_loading_percentage'.
      Removed ONLY on devices of type TD (cross-checked with the coordinator),
      never on WM/WD/AC.
    - The air purifier panel LIGHT (unique_id '<id>_panel_light'), replaced by a
      select with the same unique_id in a different domain. Scoped to the light
      domain for that reason: removing by unique_id alone would delete the
      replacement along with the entity it replaces.
    - The water heater's duplicate control surfaces (v5.21.0): the '<id>_hw_mode'
      SELECT and the '<id>_target_temp' NUMBER, superseded by the water_heater
      entity. Domain- AND type-scoped (HW/WH only, cross-checked with the
      coordinator): the wine cooler and the oven keep their own legitimate
      '<id>_target_temp' numbers.

    Without this cleanup there would be orphan 'unavailable' entities with the '?' badge.
    """
    from homeassistant.helpers import entity_registry as er

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    coord_data = getattr(coordinator, "data", None)
    td_ids = {
        appliance_id
        for appliance_id, device in (coord_data or {}).items()
        if isinstance(device, dict) and device.get("type") == APPLIANCE_TD
    }
    td_orphans = {
        f"{appliance_id}{suffix}"
        for appliance_id in td_ids
        for suffix in _TD_REMOVED_SUFFIXES
    }
    hw_ids = {
        appliance_id
        for appliance_id, device in (coord_data or {}).items()
        if isinstance(device, dict)
        and device.get("type") in (APPLIANCE_HW, APPLIANCE_WH)
    }
    hw_orphans = {
        (domain, f"{appliance_id}{suffix}")
        for appliance_id in hw_ids
        for domain, suffix in _HW_REMOVED
    }

    registry = er.async_get(hass)
    checked = 0
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        checked += 1
        unique_id = reg_entry.unique_id or ""
        domain = (reg_entry.entity_id or "").split(".", 1)[0]
        if domain == "switch" and unique_id.endswith("_power"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info("Removed legacy power switch: id=%s", redact_id(reg_entry.unique_id))
        elif domain == "light" and unique_id.endswith("_panel_light"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed legacy purifier panel light: id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif unique_id in td_orphans:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed invalid consumption entity for tumble dryer: id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif (domain, unique_id) in hw_orphans:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed retired water-heater control surface: id=%s",
                redact_id(reg_entry.unique_id),
            )
    _LOGGER.debug(
        "Setup debug: legacy cleanup completed for entry=%s, checked=%d, removed=%d",
        entry.entry_id,
        checked,
        removed,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Haier hOn integration from a Config Entry."""
    from .hon_client import HonClient

    # Silence by default the noise of the realtime MQTT attempts and register
    # the debug service. Done BEFORE the client setup so the logger is already at
    # WARNING when the MQTT client starts to (re)connect.
    _async_register_services(hass)

    # Apply the persisted debug toggles RIGHT AWAY, but AFTER _async_register_services
    # (which on the first registration silences the MQTT noise by default) so the
    # persisted MQTT toggle, if active, wins over that silencing. Applying them here
    # and not at the end of setup makes the DEBUG level cover the setup path too
    # (login, discovery, first refresh): that is exactly what one wants to trace when
    # enabling debug for discovery problems. reset_when_off=False: an OFF toggle must
    # not reset a DEBUG set at runtime via the services, which must survive the
    # retries of a failing setup (the default MQTT silencing stays guaranteed by
    # _async_register_services).
    _apply_debug_options(entry, reset_when_off=False)

    # Current entries store "email"; tolerate and migrate older/corrupt entries that
    # still carry the old "username" key so setup can recover without a reinstall.
    email = entry.data.get("email") or entry.data.get("username")
    password = entry.data.get("password")
    # Persisted refresh token (added for 2FA): runtime refreshes instead of doing a
    # full login, so an account with email-OTP is not re-challenged on every restart.
    # "" on legacy entries / non-2FA accounts -> a normal login as before.
    refresh_token = entry.data.get("refresh_token", "")

    _LOGGER.debug(
        "Setup debug: starting setup entry=%s title=%s email=%s platforms=%s scan_interval=%ss",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        _redact_email(email),
        PLATFORMS,
        SCAN_INTERVAL,
    )

    if not email:
        _LOGGER.error(
            "Missing credentials in the config entry ('email' key absent). "
            "Remove and reconfigure the integration."
        )
        return False
    if "email" not in entry.data and entry.data.get("username"):
        # Drop the legacy "username" key in the same update so the migrated entry
        # data carries only "email" (no stale key left for diagnostics/iteration).
        migrated = {k: v for k, v in entry.data.items() if k != "username"}
        migrated["email"] = email
        hass.config_entries.async_update_entry(entry, data=migrated)

    hon_client = HonClient(email=email, password=password, refresh_token=refresh_token)

    # Initial client setup in executor (does not block HA's event loop)
    try:
        _LOGGER.debug("Setup debug: running HonClient.setup_sync in executor")
        await hass.async_add_executor_job(hon_client.setup_sync)
        _LOGGER.debug("Setup debug: HonClient.setup_sync completed")
    except asyncio.CancelledError:
        await _async_close_client(hon_client)
        raise
    except Exception as err:
        # A background setup cannot prompt for a 2FA code: an MFA challenge (carried
        # MFA_REQUIRED -> requires_reauth) is routed by _raise_setup_error to
        # ConfigEntryAuthFailed -> the reauth flow, which CAN prompt for the OTP.
        _LOGGER.error("Unable to connect to hOn: %s", err)
        await _async_close_client(hon_client)
        _raise_setup_error(err)

    # Persist a rotated refresh token so the next restart keeps skipping the full login
    # (and the 2FA prompt). Single helper, change-guarded (see _persist_refresh_token).
    _persist_refresh_token(hass, entry, hon_client)

    async def async_update_data() -> dict:
        """Fetch the updated data from all the hOn devices."""
        try:
            _LOGGER.debug("Coordinator debug: starting hOn data update")
            data = await hon_client.async_get_appliances_data()
            # A runtime token refresh / background re-auth may have rotated the refresh
            # token during this fetch; persist it (only on a real change) so it survives a
            # restart -- not just the initial setup.
            _persist_refresh_token(hass, entry, hon_client)
            summary = [
                {
                    "id": redact_mac(appliance_id),
                    "name": redact_id(appliance_data.get("name")),
                    "type": appliance_data.get("type"),
                    "mac": redact_mac(appliance_data.get("mac")),
                    "attributes": len(appliance_data.get("attributes", {}))
                    if isinstance(appliance_data.get("attributes"), dict)
                    else 0,
                    "settings": len(appliance_data.get("settings", {}))
                    if isinstance(appliance_data.get("settings"), dict)
                    else 0,
                }
                for appliance_id, appliance_data in data.items()
            ]
            _LOGGER.debug(
                "Coordinator debug: hOn data update completed, devices=%d summary=%s",
                len(data),
                summary,
            )
            return data
        except Exception as err:
            from .error_codes import classify

            hon_client.last_error_code = classify(err)
            # Attribute the phase to THIS update failure (a carried HonCodedError.phase, or
            # the live auth phase if a re-auth was in flight) so diagnostics never shows a
            # phase/mfa-summary left over from the last login event.
            hon_client.last_error_phase = (
                getattr(err, "phase", None)
                or getattr(hon_client._hon_instance, "auth_phase", "")
                or None
            )
            hon_client.last_mfa_summary = None
            _LOGGER.debug(
                "Coordinator debug: hOn data update failed [%s]: %s",
                hon_client.last_error_code.label,
                err,
                exc_info=True,
            )
            _raise_update_error(err)

    stored = False
    try:
        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Haier hOn data",
            update_method=async_update_data,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        # First fetch
        _LOGGER.debug("Setup debug: first coordinator refresh at startup")
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug(
            "Setup debug: first refresh completed, last_update_success=%s devices=%d",
            getattr(coordinator, "last_update_success", None),
            len(coordinator.data) if isinstance(coordinator.data, dict) else 0,
        )
        coordinator.hon_client = hon_client

        # Program-label catalog (#71). The appliance schema names a program with its
        # i18n KEY (`PROGRAMS.WM_WD.HQD_AUTOCLEAN` -> slug `hqd_autoclean`), so readable
        # names only exist in the catalog the hOn app downloads. Fetched ONCE here and
        # parked on the coordinator, so no entity ever does I/O for a label. Best-effort
        # by construction: async_load absorbs every failure and returns an empty catalog,
        # in which case the entities keep showing the raw code.
        setattr(
            coordinator,
            program_labels.COORDINATOR_ATTR,
            await program_labels.async_load(hass),
        )

        # Realtime: wire MQTT pushes to the coordinator (#4). Without this the push
        # channel was inert and entities only refreshed on the 60s poll. The push
        # arrives on the awscrt thread, so the snapshot is built THERE (a coherent
        # intra-thread read of the just-mutated appliances) and the publish is hopped
        # onto the HA event loop via call_soon_threadsafe; async_set_updated_data
        # publishes WITHOUT triggering a new poll (the 60s poll stays as a
        # reconciliation safety-net). Detached on unload.
        @callback
        def _publish_realtime(snapshot: dict) -> None:
            if snapshot:
                coordinator.async_set_updated_data(snapshot)

        def _on_realtime_push(_arg) -> None:
            # Runs on the awscrt thread; must never let an exception reach it.
            try:
                snapshot = hon_client.build_realtime_snapshot()
                hass.loop.call_soon_threadsafe(_publish_realtime, snapshot)
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Setup debug: realtime push handling failed: %s", err)

        try:
            hon_client.subscribe_updates(_on_realtime_push)
            entry.async_on_unload(lambda: hon_client.subscribe_updates(None))
            _LOGGER.debug("Setup debug: realtime MQTT push wired to coordinator")
        except Exception as err:  # pragma: no cover - realtime is best-effort
            _LOGGER.warning("Setup debug: could not wire realtime MQTT push: %s", err)

        # Integration version, for the diagnostics device's sw_version ("Firmware:"
        # row on the device card). Lazy import so the test stubs that import this
        # package do not need to stub homeassistant.loader; tolerant if unavailable.
        integration_version: str | None = None
        try:
            from homeassistant.loader import async_get_integration

            integration = await async_get_integration(hass, DOMAIN)
            integration_version = str(integration.version)
        except Exception as err:  # pragma: no cover - non-critical, cosmetic only
            _LOGGER.debug("Setup debug: could not resolve integration version: %s", err)

        # FIX: store both the coordinator and the client in the structure expected by all platforms
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "coordinator": coordinator,
            "client": hon_client,
            "integration_version": integration_version,
            # Baseline for _async_options_updated: the options already in effect at
            # the start of setup, so a later data-only entry write (token rotation) is
            # a no-op and only a real options change re-applies the levels or reloads.
            _ENTRY_OPTS_KEY: _entry_opts(entry),
        }
        stored = True
        _LOGGER.debug("Setup debug: coordinator and client stored in hass.data for entry=%s", entry.entry_id)

        # Legacy entity cleanup (e.g. the removed "Power" switch): it must never
        # block the setup, so we absorb any registry errors.
        try:
            _remove_legacy_entities(hass, entry)
        except Exception as err:
            _LOGGER.debug("Legacy entity cleanup failed: %s", err)

        _LOGGER.debug("Setup debug: forwarding platforms %s", PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Setup debug: platform forwarding completed")
    except asyncio.CancelledError:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after cancelled setup: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise
    except Exception:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after failed setup: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise

    # Setup succeeded: register a listener that re-applies the debug toggles on the
    # fly when they change (async_on_unload removes the listener when the entry is
    # unloaded, without a reload). The levels have already been applied at the start
    # of setup; here it only remains to hook up the on-the-fly update.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry when the integration is disabled."""
    _LOGGER.debug("Unload debug: unloading entry=%s platforms=%s", entry.entry_id, PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("Unload debug: async_unload_platforms result=%s", unload_ok)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            _LOGGER.debug("Unload debug: closing HonClient for entry=%s", entry.entry_id)
            await _async_close_client(client)
        else:
            _LOGGER.debug("Unload debug: no HonClient to close for entry=%s", entry.entry_id)
        # Last entry removed: remove the global debug services.
        if not hass.data.get(DOMAIN):
            for service in (
                SERVICE_SET_MQTT_LOG_LEVEL,
                SERVICE_SET_LOG_LEVEL,
                SERVICE_REFRESH,
                SERVICE_SEND_COMMAND,
                SERVICE_PROBE_SETTINGS_OPERATION,
            ):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
                    _LOGGER.debug("Unload debug: removed service %s", service)
    return unload_ok
