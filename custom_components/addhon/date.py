# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Date entities (Tier 3): the water heater's scheduled vacation window.

Ground truth (HA diagnostics dump of a real HP250M7C-F9, 2026-08, vacation set in
the app for 2026-08-18 -> 2026-08-22): the hOn app schedules holiday BY DATES, as
an operation of the `settings` command -- `operationName` fixed to "grSetVacDate"
with `vacStartDate` / `vacEndDate` carrying plain ISO dates ("2026-08-18"). The
dump shows the startProgram program still `auto` while the window sits in the
future: the DEVICE enters the holiday program by itself inside the window. This
is therefore DISTINCT from the water_heater entity's away toggle, which starts
the `vac` program IMMEDIATELY via startProgram; the two compose (schedule a
window here, or force holiday now with away).

READ PATH: the cloud shadow reports vacStartDate/vacEndDate as plain attributes,
refreshed on every poll, so a window set or moved in the app lands here without
any extra plumbing.

WRITE PATH: the generic settings sender (hon_commands.async_send_command), the
SAME proven path every settings write on this device uses. `command.send()`
transmits the whole parameters group, so `operationName` (a fixed parameter
already holding "grSetVacDate") and the untouched sibling date ride along; the
sibling's value is poll-fresh because the engine syncs shadow -> settings on
every update (appliance.sync_params_to_command). The two parameters are
typology "fixed" in the schema, which the engine deliberately treats as
writable (HonParameterFixed's setter does not validate).

The only write-side guard is the pair ordering (start <= end): the device's own
handling of an inverted window is unknown, so it is refused up front with a
clear message instead of being discovered on the appliance. Everything else --
past dates, how the window is cleared -- is left to the device/app semantics
rather than guessed here.

Capability-gated like every control in this integration: an entity exists only
when the device exposes the parameter in a write command, so a water heater
without app-scheduled holiday simply gets no date entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging

from homeassistant.components.date import DateEntity, DateEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .const import APPLIANCE_HW, APPLIANCE_WH, DOMAIN
from .debug_utils import redact_id
from .hon_commands import async_send_command, find_settings_param

_LOGGER = logging.getLogger(__name__)

# The two halves of the window, as the real HP250M7C-F9 spells them.
VAC_START_PARAM = "vacStartDate"
VAC_END_PARAM = "vacEndDate"

# How the appliance spells "no window scheduled". Ground truth: the 2026-08 captures show
# a real window (2026-08-18 -> 2026-08-22, with machMode on the vac program) and then
# BOTH halves back at this date once it was cleared from the app, with machMode back to
# the normal program. It is not a date anyone schedules a holiday for, so it is reported
# as no value at all rather than as a holiday in the year 2000.
VAC_UNSET_DATE = "2000-01-01"


@dataclass(frozen=True, kw_only=True)
class HonDateEntityDescription(DateEntityDescription):
    """Description of a Haier hOn date.

    - `key` = unique_id suffix AND translation_key.
    - `param` = the hOn parameter read (shadow attribute) and written (settings).
    - `is_start` = which half of the vacation window this is, for the ordering
      guard (the sibling is simply the other VAC_*_PARAM).
    """

    param: str
    is_start: bool


_VACATION_DATES: tuple[HonDateEntityDescription, ...] = (
    HonDateEntityDescription(
        key="vacation_start_date",
        param=VAC_START_PARAM,
        is_start=True,
        icon="mdi:calendar-start",
        entity_category=EntityCategory.CONFIG,
    ),
    HonDateEntityDescription(
        key="vacation_end_date",
        param=VAC_END_PARAM,
        is_start=False,
        icon="mdi:calendar-end",
        entity_category=EntityCategory.CONFIG,
    ),
)

# HW is the ground-truthed type; WH is included so a plain water heater that DOES
# expose the window gets the entities, and one that does not simply fails the gate.
DATES: dict[str, tuple[HonDateEntityDescription, ...]] = {
    APPLIANCE_HW: _VACATION_DATES,
    APPLIANCE_WH: _VACATION_DATES,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the date entities only for the parameters the device exposes as writable."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities: list[HonVacationDate] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        app_type = data.get("type", "")
        appliance = data.get("appliance")
        created: list[str] = []
        for description in DATES.get(app_type, ()):
            found = find_settings_param(appliance, description.param)
            if found is None:
                continue
            command_name, _param = found
            entities.append(
                HonVacationDate(
                    coordinator, appliance_id, description, command_name, client
                )
            )
            created.append(description.key)
        if DATES.get(app_type):
            _LOGGER.debug(
                "Date debug: '%s' (type=%s, id=%s) -> %d/%d dates %s",
                data.get("name", "Haier"),
                app_type,
                redact_id(appliance_id),
                len(created),
                len(DATES.get(app_type, ())),
                created,
            )
    async_add_entities(entities)


class HonVacationDate(HonBaseEntity, DateEntity):
    """One half of the water heater's scheduled vacation window."""

    entity_description: HonDateEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonDateEntityDescription,
        command_name: str,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self.entity_description = description
        self._command_name = command_name
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        _LOGGER.debug(
            "Date debug: init '%s' id=%s param=%s cmd=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            description.param,
            command_name,
        )

    def _shadow_date(self, param: str) -> date | None:
        """Shadow attribute as a date, or None when absent, unparseable or UNSET.

        The unset sentinel is folded in here on purpose, so every caller -- the state, the
        ordering guard and the pair completion below -- agrees on what "no window" means
        and none of them has to special-case it.
        """
        raw = self._get_attr(param)
        if raw is None:
            return None
        text = str(raw).strip()
        if text == VAC_UNSET_DATE:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            _LOGGER.debug("Date debug: '%s' not an ISO date raw=%r", param, raw)
            return None

    @property
    def native_value(self) -> date | None:
        return self._shadow_date(self.entity_description.param)

    def _validate_window(self, value: date) -> None:
        """Refuse an inverted window (start after end) against the poll-fresh sibling.

        The sibling is read from the SHADOW, not the command object, so a window
        moved in the app since the last write is compared against, not clobbered
        over. A sibling the device does not report (or reports unparseable) skips
        the guard rather than blocking the write: without both halves there is no
        window to invert.
        """
        description = self.entity_description
        sibling = self._shadow_date(
            VAC_END_PARAM if description.is_start else VAC_START_PARAM
        )
        if sibling is None:
            return
        start, end = (value, sibling) if description.is_start else (sibling, value)
        if start > end:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vacation_dates_inverted",
                translation_placeholders={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )

    async def async_set_value(self, value: date) -> None:
        """Write one half of the window through the settings command."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        self._validate_window(value)
        description = self.entity_description
        param = description.param
        send_value = value.isoformat()
        params = {param: send_value}
        # Setting one half of a window that does not exist yet writes BOTH, as a one-day
        # window the user then stretches. Without this the entities dead-lock each other:
        # the ordering guard refuses a start later than the (unset) end, so a window could
        # only ever be started from its END date -- which is not how anyone thinks about
        # booking a holiday.
        sibling = VAC_END_PARAM if description.is_start else VAC_START_PARAM
        if self._shadow_date(sibling) is None:
            params[sibling] = send_value
            _LOGGER.debug(
                "Date debug: %s was unset, opening a one-day window on %s id=%s",
                sibling,
                send_value,
                redact_id(self._appliance_id),
            )
        try:
            _LOGGER.debug(
                "Date debug: set %s (cmd=%s) id=%s",
                params,
                self._command_name,
                redact_id(self._appliance_id),
            )
            await async_send_command(
                self.hass, client, appliance, self._command_name, params
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Date: set error %s=%s: %s", param, send_value, err, exc_info=True
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
