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

CURRENT STATUS ON THE HP250M7C-F9, honestly: the write is ACCEPTED by the cloud, echoes
back for about a minute, and is then DISCARDED by the appliance -- the whole schedule
block reverts on the next report. The day-mask theory (v5.27) was disproved by the
shadow history: `opp1EcoDays` read "7F" on every one of seven captures, so the mask was
never broken appliance-side.

THE LEADING EXPLANATION is the PROGRAM. The parameters are named `opp1ECO...` and the
appliance's own panel carries the schedule UI ("Programacao horaria" -- heating only
within the defined window, same every day or per-day) in the ECO program's menus, while
this unit runs `auto` (machMode 1) -- found by the user on the physical panel,
2026-08-26. A window configured for a program that is not running is configuration the
firmware has no owner for, and discarding it on the next state publish is exactly what
was observed. The test is cheap and uses only proven writes: switch the water_heater to
Eco (startProgram, solid), write the window, watch whether it survives the next polls.
The panel's "different heating schedules per day" option is also what the two period
groups and the day mask exist for -- per-weekday windows become reachable the moment the
base case works.

THE DAY MASK still rides along defensively: when the reported mask selects no days the
write restores "7F" (every day) in the same payload, bypassing the mistyped range
setter via payload_overrides. A mask that selects days is left exactly as it is.

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
)
from .hw_values import (
    HW_ECO_DAYS_ALL,
    HW_ECO_DAYS_ATTR,
    hw_eco_days,
    hw_time,
)

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
    # True for the heating-window pair: the write must keep the day mask alive, or the
    # appliance sanitizes the whole window away (see the module docstring).
    restores_day_mask: bool = False


def _window(key: str, param: str, *, icon: str, enabled_default: bool,
            restores_day_mask: bool = False):
    return HonTimeEntityDescription(
        key=key,
        param=param,
        icon=icon,
        entity_category=EntityCategory.CONFIG,
        enabled_default=enabled_default,
        restores_day_mask=restores_day_mask,
    )


# The heating window, then the quiet windows (disabled by default). See the module
# docstring for why exactly one heating window is offered.
_SCHEDULE_TIMES: tuple[HonTimeEntityDescription, ...] = (
    _window(
        "heating_window_start",
        "opp1EcoStartTime1",
        icon="mdi:calendar-clock",
        enabled_default=True,
        restores_day_mask=True,
    ),
    _window(
        "heating_window_end",
        "opp1EcoEndTime1",
        icon="mdi:calendar-clock",
        enabled_default=True,
        restores_day_mask=True,
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
        # A window whose day mask selects no days is sanitized away by the appliance's
        # own firmware (times reset to 00:00 on the next poll, and at power-on). The
        # mask is mistyped by the cloud schema, so it cannot go through the parameter
        # setter; it rides in the payload directly. "7F" = every day, the only value
        # ever observed. A mask that already selects days is left exactly as it is.
        payload_overrides: dict[str, str] | None = None
        if (
            self.entity_description.restores_day_mask
            and hw_eco_days(self._get_attr(HW_ECO_DAYS_ATTR)) is None
        ):
            payload_overrides = {HW_ECO_DAYS_ATTR: HW_ECO_DAYS_ALL}
        try:
            _LOGGER.debug(
                "Time debug: set %s=%s (cmd=%s, mask_restore=%s) id=%s",
                param,
                send_value,
                self._command_name,
                payload_overrides is not None,
                redact_id(self._appliance_id),
            )
            await async_send_command(
                self.hass,
                client,
                appliance,
                self._command_name,
                {param: send_value},
                payload_overrides=payload_overrides,
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
