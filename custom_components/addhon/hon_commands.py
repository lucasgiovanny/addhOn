# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared helpers to send hOn commands to the controls (Tier 3).

Generalizes the pattern already used by button.py (sending a command while
applying parameter overrides) and by ac_command.async_send_settings (set on the
write command), making it neutral with respect to the command name. The Tier 3
controls (number, switch/select/button for fridge/oven/...) reuse it without
duplicating lookup, rollback and execution on the client's dedicated loop.

Gating principle (see memory/repo): every control is CAPABILITY-GATED, i.e. it is
created only if the device ACTUALLY exposes the command + parameter (the client runtime
schema), with the candidate superset seeded from the app mapping. This way it is
validated where we have the real dump, broad for the other models, and safe
everywhere (a missing parameter does not generate an entity).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
import logging

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .param_rollback import restore_params, snapshot_params

_LOGGER = logging.getLogger(__name__)

# the hOn commands from which the "set" controls (number/switch/select-mode) read
# and write the free parameters. The client names the command after the device's
# top-level key: "settings" is the AC's and the real fridge's one (the active
# category exposes setParameters); "setParameters" as a fallback for other models.
SETTINGS_COMMANDS: tuple[str, ...] = ("settings", "setParameters")


def get_commands(appliance) -> dict:
    """Command dictionary of the device, or {} if absent/invalid."""
    commands = getattr(appliance, "commands", None)
    return commands if isinstance(commands, dict) else {}


def get_command(appliance, name: str):
    """Command `name`, or None."""
    return get_commands(appliance).get(name)


def command_param(appliance, command_name: str, param_name: str):
    """Parameter `param_name` of command `command_name`, or None if absent."""
    command = get_command(appliance, command_name)
    params = getattr(command, "parameters", None) if command is not None else None
    if isinstance(params, dict):
        return params.get(param_name)
    return None


def find_settings_param(
    appliance, param_name: str, command_names: Sequence[str] = SETTINGS_COMMANDS
):
    """Search for `param_name` among the `command_names` commands (in order).

    Returns (command_name, param) of the first match, or None. It is the
    capability-gate of the controls that write to a settings/setParameters command.
    """
    for name in command_names:
        param = command_param(appliance, name, param_name)
        if param is not None:
            return name, param
    return None


# The parameter through which a settings-style command declares WHICH device operation
# it performs. Where it exists the command is a SINGLE-OPERATION envelope: `command.send()`
# transmits the whole parameter group, but the appliance acts only on the fields that
# belong to `operationName` and silently drops everything else in the same payload.
SETTINGS_OPERATION_PARAM = "operationName"

# Parameters each KNOWN operation actually writes. An operation absent from this table is
# never treated as blocking: "not ground-truthed" is not "writes nothing".
SETTINGS_OPERATION_PARAMS: dict[str, frozenset[str]] = {
    # Heat pump water heater (HP250M7C-F9): the app schedules holiday BY DATES through
    # this operation, and it is the operation the cloud pins the whole `settings` command
    # to on that model. Live-verified in both directions: a vacation window written from
    # Home Assistant reaches the device (v5.19.0), while a tempSel (v5.10) and an
    # onOffStatus (v5.22.0) sent in the very same payload are silently dropped.
    "grSetVacDate": frozenset({"vacStartDate", "vacEndDate"}),
}


def settings_operation(appliance, command_name: str = "settings") -> str | None:
    """The operation `command_name` is PINNED to, or None when it is a free write.

    Pinned means `operationName` exists AND offers exactly one value (typology "fixed",
    or an enum with a single member). Ground truth: on a real HP250M7C-F9 the `settings`
    command carries `operationName` fixed to "grSetVacDate", identical across four
    diagnostics dumps a month apart -- it is the cloud's command definition, not a
    leftover of the last app action.

    Returns None when the parameter is absent or offers a choice, so an appliance whose
    settings command is a genuine multi-parameter write (the air conditioner, the wine
    cooler) is never gated by this.
    """
    param = command_param(appliance, command_name, SETTINGS_OPERATION_PARAM)
    if param is None:
        return None
    if len(param_values(param)) > 1:
        return None
    value = getattr(param, "value", None)
    text = "" if value is None else str(value).strip()
    return text or None


def settings_write_blocked(
    appliance, param_name: str, command_name: str = "settings"
) -> str | None:
    """The pinned operation that would SWALLOW a write of `param_name`, or None.

    The capability gate for every settings-backed control: a device whose settings
    command only performs one KNOWN operation cannot be written through it for anything
    outside that operation, and a control that reports success while the appliance
    ignores it is worse than no control at all.
    """
    operation = settings_operation(appliance, command_name)
    if operation is None:
        return None
    carried = SETTINGS_OPERATION_PARAMS.get(operation)
    if carried is None or param_name in carried:
        return None
    return operation


def param_values(param) -> list[str]:
    """Allowed values (strings) of an enum parameter, or [] if not enumerated."""
    values = getattr(param, "values", None)
    if isinstance(values, (list, tuple)):
        return [str(v) for v in values]
    return []


def param_range(param) -> tuple[float, float, float] | None:
    """(min, max, step) of a range parameter, or None if it is not a range.

    Duck-typing on min/max/step (HonParameterRange exposes them). step returns 1.0
    if the parameter reports it as 0 (no declared increment)."""
    if not all(hasattr(param, attr) for attr in ("min", "max", "step")):
        return None
    try:
        lo = float(param.min)
        hi = float(param.max)
        step = float(param.step) or 1.0
    except (TypeError, ValueError):
        return None
    if hi < lo:
        return None
    if step <= 0:  # non-positive increment: inconsistent range for a numeric control
        return None
    return lo, hi, step


def snap_to_range(
    value: float, rng: tuple[float, float, float] | None, param=None
) -> float:
    """`value` clamped into [min, max] AND snapped onto the parameter's min/step grid.

    Home Assistant's temperature controls do NOT enforce the step: the more-info dial
    seeds itself from the entity state and adds/subtracts the step to it, so a device
    that reports an OFF-GRID setpoint in its cloud shadow (a real HP250M7C-F9 reported
    tempSel 59.2 on a range[35,75,1]) makes every +/- press produce an off-grid request
    (60.2, 61.2, ...). The engine's range setter rightly rejects those, and the user only
    sees "Allowed: min 35 max 75 step 1 But was: 60.2" -- the setpoint never moves.

    So the write path snaps: the user asked for the neighbouring setpoint, and the nearest
    grid point IS that setpoint. Snapping lives HERE, on the way out of the entity, never
    in the setter: every write path relies on the setter's ValueError to trigger the
    parameter rollback (see async_send_command), so the setter must keep rejecting.

    `param` is the resolved runtime parameter, when the caller has it: a HonParameterRange
    carries `snap_to_grid`, the SAME grid math (epsilon, floored top index) the engine
    validates with, so acceptance and snapping cannot disagree. It is called on the
    already-clamped value because it rejects (by contract) anything out of [min, max].
    A parameter without it -- a duck-typed range -- takes the arithmetic path below.

    `rng` MUST be the range the DEVICE declares (param_range), never a UI fallback: with
    no declared grid there is nothing to snap to, and rounding onto a guessed one would
    destroy a value the device would have accepted (a half degree on a plain, unvalidated
    parameter). `None` therefore returns the value unchanged.
    """
    wanted = float(value)
    if rng is None:
        return wanted
    lo, hi, step = rng
    clamped = min(max(wanted, lo), hi)
    snapper = getattr(param, "snap_to_grid", None)
    if callable(snapper):
        try:
            return _clean_grid_value(float(snapper(clamped)), lo, step)
        except (TypeError, ValueError):
            pass  # duck-typed parameter or stale bounds: fall through to the arithmetic
    if step <= 0:  # no grid to snap to (malformed increment): clamping is all we can do
        return clamped
    # Mirrors HonParameterRange.snap_to_grid: nearest index, top index FLOORED so the
    # snapped value can never exceed max, same magnitude-scaled epsilon capped to step/4.
    eps = min(1e-9 * max(1.0, abs(lo), abs(hi), abs(step)), step / 4)
    max_index = max(0, int((hi - lo + eps) / step))
    index = min(max_index, max(0, round((clamped - lo) / step)))
    return _clean_grid_value(min(hi, lo + index * step), lo, step)


def _clean_grid_value(value: float, lo: float, step: float) -> float:
    """Grid point rounded to its OWN precision, killing `lo + n*step` float drift.

    A grid point carries at most max(decimals(lo), decimals(step)) decimals, so rounding
    there is exact -- it never collapses two distinct points. Without it the serialized
    string can read "16.299999999999999", which the setter still accepts (its epsilon
    tolerates the drift) but which reaches the cloud verbatim.
    """
    ndigits = max(_decimals(lo), _decimals(step))
    return round(value, ndigits)


def _decimals(number: float) -> int:
    """Fractional-digit count of a float (0.1 -> 1, 1.0 -> 0), via its shortest repr."""
    try:
        exponent = Decimal(str(float(number))).normalize().as_tuple().exponent
    except (ArithmeticError, TypeError, ValueError):
        return 0
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


async def async_send_command(
    hass,
    client,
    appliance,
    command_name: str,
    params: dict,
    *,
    pre_send: Callable[[dict], None] | None = None,
) -> None:
    """Apply `params` (name->value) to command `command_name` and send it on
    the client's dedicated loop, with rollback if an assignment fails.

    `pre_send(command_params)`: optional hook run BEFORE applying the requested
    parameters (the AC uses it to sanitize windDirection*). The requested values
    win anyway over whatever pre_send has set.
    """
    if not appliance or not client:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="appliance_or_client_unavailable",
        )

    def _do_send():
        async def _inner():
            command = get_command(appliance, command_name)
            if command is None:
                raise RuntimeError(
                    f"Command '{command_name}' not found on the device"
                )
            command_params = getattr(command, "parameters", {})
            if not isinstance(command_params, dict):
                command_params = {}
            missing = [key for key in params if key not in command_params]
            if missing:
                raise RuntimeError(
                    f"Parameter(s) not found in command {command_name}: "
                    + ", ".join(missing)
                )
            # Snapshot of the complete internal state of EVERY parameter BEFORE pre_send.
            # Assigning a trigger parameter fires the rules, which mutate the siblings
            # (value AND values/min/max); on a pre_send or send() failure we restore the
            # full pre-mutation state via the shared param_rollback helper (copies
            # __dict__ directly, so rules are not re-fired and values/min/max come back).
            snapshots = snapshot_params(command_params)
            try:
                if pre_send is not None:
                    pre_send(command_params)
                for key, value in params.items():
                    command_params[key].value = value
                    _LOGGER.debug("Command %s: '%s' = %s", command_name, key, value)
                await command.send()
            except Exception:
                restore_params(command_params, snapshots)
                raise
            _LOGGER.debug(
                "Command %s: send completed (params=%s)", command_name, list(params)
            )

        client.run_command_sync(_inner())

    await hass.async_add_executor_job(_do_send)
