# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Time entities (Tier 3): the water heater's own schedule windows.

WHY THESE EXIST. The appliance schedules itself -- off-peak ("cheap energy") windows,
quiet windows, a daily power timer -- and until v5.26.0 none of it could be changed from
Home Assistant. The reason was thought to be the `settings` command's `operationName`,
which the cloud pins to a single operation; it turned out to be simpler and better.

WHAT ACTUALLY DECIDES. A pinned settings command writes its MANDATORY parameter group
and drops the rest. Five live data points on a real HP250M7C-F9 agree without exception
(see hon_commands.settings_write_blocked), and the sixth confirmed it in the direction
that matters: an off-peak window written straight through `settings` -- no operation name
touched -- landed on the appliance and came back as 11:00-16:00 (2026-08-25). Every
schedule field is mandatory on that schema; every toggle that never worked is not.

WHAT IS EXPOSED. ONE heating window -- the first slot of off-peak period group 1. One
window, not the nine slots the appliance carries, is a deliberate product decision: the
job to be done is "heat during my solar / cheap-tariff hours", and one window does it.

HOW THE RIGHT SLOT WAS FOUND -- a short archaeology, kept because every wrong theory
eliminated something real. Writes to period group 1 were accepted by the cloud, echoed
for a minute, and then discarded by the appliance. The operation-name theory died on
the mandatory-flag experiments (v5.26); the day-mask theory died on the shadow history
(opp1EcoDays read "7F" on all seven captures, v5.28); the eco-program theory died live
(the group-1 write reverted in eco mode too). What settled it was setting the window on
the APPLIANCE'S OWN PANEL and diffing the next capture: the panel's window landed in
GROUP 2, with nothing else changing. Group 1 -- the one with the opp1EcoDays mask,
sibling to the offpeakSignal* dry-contact config -- belongs to the off-peak TARIFF
feature, inactive on this unit (powerSupplySource 0). Two subsystems, two groups; we
were writing to the wrong one.

The panel offers "same every day" vs "different heating schedules per day"; how the
per-day variant maps onto the slots is not yet captured -- a panel experiment plus one
diff away, the same way the base case was cracked.

WHAT IS NOT EXPOSED, AND WHY:
- the other off-peak slots and period group 2: see above. For group 2 additionally,
  nothing in any capture says what selects it (`offPeakPeriodScheme` is a bare 0/1 that
  has read 1 throughout).
- the daily power timer (`timingPowerOn` / `timingPowerOff`) is mandatory as well, but
  the cloud declares both as range[0,1] while the appliance reports "00:00". A time
  cannot be assigned to that parameter at all -- the engine's range setter refuses it --
  so there is nothing to write through. The pair is preserved on every other write
  instead (hon_commands.shadow_overrides) and stays readable as sensors.
- the day mask (`opp1EcoDays`) has the same mistyped-schema problem, and on top of it
  only its all-days value has ever been observed, so its bit order is unknown.

An unset slot is 00:00 on BOTH ends. Setting the two ends to the same time is therefore
how the appliance is told to forget a window, and the state reads as 00:00 rather than as
"unknown" -- unlike the holiday dates, a time entity has no empty state to fall back on,
and 00:00 is what the appliance genuinely reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time
import logging

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .const import APPLIANCE_HW, APPLIANCE_WH, DOMAIN
from .debug_utils import redact_id
from .hon_commands import (
    async_send_command,
    find_settings_param,
    settings_write_blocked,
    with_operation,
)
from .hw_values import hw_time

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonTimeEntityDescription(TimeEntityDescription):
    """Description of a Haier hOn schedule time.

    - `key` = unique_id suffix AND translation_key.
    - `param` = the hOn parameter read (shadow attribute) and written (settings). The
      appliance spells both sides identically for these, unlike the auxiliary setpoints.
    """

    param: str
    enabled_default: bool = True


def _window(key: str, param: str, *, icon: str, enabled_default: bool):
    return HonTimeEntityDescription(
        key=key,
        param=param,
        icon=icon,
        entity_category=EntityCategory.CONFIG,
        enabled_default=enabled_default,
    )


# The heating window: PERIOD GROUP 2, slot 1. Not a guess -- a window set on the
# appliance's own panel ("Programacao horaria", 09:00-18:00, 2026-08-26) landed in
# opp2EcoStartTime1/opp2EcoEndTime1 in the very next capture, with NOTHING else changing
# (no enable flag, no scheme, no mask). Group 1, which v5.26-v5.28 wrote to and the
# appliance kept discarding, belongs to the off-peak TARIFF feature (its day mask is
# opp1EcoDays, its siblings are the offpeakSignal* dry-contact config), which is
# inactive on this unit.
_SCHEDULE_TIMES: tuple[HonTimeEntityDescription, ...] = (
    _window(
        "heating_window_start",
        "opp2EcoStartTime1",
        icon="mdi:calendar-clock",
        enabled_default=True,
    ),
    _window(
        "heating_window_end",
        "opp2EcoEndTime1",
        icon="mdi:calendar-clock",
        enabled_default=True,
    ),
)
# The quiet-window entities v5.26 added were removed in v5.28 (registry cleanup in
# __init__): they belong to the same inactive subsystem, their writes are discarded the
# same way, and unlike the heating window nobody is trying to make them work.

# HW is the ground-truthed type; WH is included so a plain water heater that DOES expose
# the schedule gets the entities, and one that does not simply fails the gate.
TIMES: dict[str, tuple[HonTimeEntityDescription, ...]] = {
    APPLIANCE_HW: _SCHEDULE_TIMES,
    APPLIANCE_WH: _SCHEDULE_TIMES,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create a time entity per schedule slot the appliance exposes as WRITABLE."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities: list[HonScheduleTime] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        app_type = data.get("type", "")
        appliance = data.get("appliance")
        created: list[str] = []
        skipped: list[str] = []
        for description in TIMES.get(app_type, ()):
            found = find_settings_param(appliance, description.param)
            if found is None:
                continue
            command_name, _param = found
            # Gated on the WRITE actually landing, not just on the parameter existing: a
            # settings command pinned to an operation that would swallow this parameter
            # produces no entity rather than a control the appliance ignores.
            blocked_by = settings_write_blocked(
                appliance, description.param, command_name
            )
            if blocked_by is not None:
                skipped.append(description.key)
                continue
            entities.append(
                HonScheduleTime(
                    coordinator, appliance_id, description, command_name, client
                )
            )
            created.append(description.key)
        if TIMES.get(app_type):
            _LOGGER.debug(
                "Time debug: '%s' (type=%s, id=%s) -> %d/%d times %s; skipped %s",
                data.get("name", "Haier"),
                app_type,
                redact_id(appliance_id),
                len(created),
                len(TIMES.get(app_type, ())),
                created,
                skipped,
            )
    async_add_entities(entities)


class HonScheduleTime(HonBaseEntity, TimeEntity):
    """One end of one schedule window."""

    entity_description: HonTimeEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonTimeEntityDescription,
        command_name: str,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self.entity_description = description
        self._command_name = command_name
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        self._attr_entity_registry_enabled_default = description.enabled_default

    @property
    def native_value(self) -> dt_time | None:
        """The slot as the appliance reports it, or None when it is not a clock reading.

        Shares hw_time with the schedule sensors, so a card and this control can never
        disagree about what the appliance said.
        """
        text = hw_time(self._get_attr(self.entity_description.param))
        if text is None:
            return None
        hour, _, minute = text.partition(":")
        return dt_time(int(hour), int(minute))

    async def async_set_value(self, value: dt_time) -> None:
        """Write one end of the window through the settings command."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        param = self.entity_description.param
        # Seconds are dropped on purpose: the appliance's whole schedule is minute
        # resolution ("00:00", "22:00") and sending "11:30:00" would not match the shape
        # it reports back, leaving the entity looking like the write failed.
        send_value = f"{value.hour:02d}:{value.minute:02d}"
        try:
            _LOGGER.debug(
                "Time debug: set %s=%s (cmd=%s) id=%s",
                param,
                send_value,
                self._command_name,
                redact_id(self._appliance_id),
            )
            # with_operation names grSetEcoTime in the payload -- the operation that
            # EXECUTES the window write (probed 2026-08-26). Without it the appliance
            # discards the field after a shadow echo, which is the whole v5.26-v5.29
            # saga in one line.
            await async_send_command(
                self.hass,
                client,
                appliance,
                self._command_name,
                with_operation(appliance, self._command_name, {param: send_value}),
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Time: set error %s=%s: %s", param, send_value, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
