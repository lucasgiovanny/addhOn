# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command loader.

Loads the three cloud streams in parallel (commands / favourites / command-history)
via the api, builds the `HonCommand`s, applies favourites and
restores the last executed state of each command.

`api`/`appliance` duck-typed.

enum-casing note (to re-validate LIVE): the favourites
(`_update_base_command_with_data`) and recover (`_recover_last_command_states`) paths
write RAW values (saved by the cloud/the history) into the parameters, which may have a
casing different from the `enumValues`. On an enum the setter accepts the value if the
normalized form matches and keeps the raw one in `intern_value`; the
`suppress(ValueError)` guards the rare value that cannot be normalized to an allowed
one (the default is kept). This is not verifiable offline (the fridge has no
favourites, the AC is offline): the decision is DEFERRED to live validation. On
already-clean values (the common case) the behavior is unchanged.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import copy
from typing import Any, Optional

from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.fixed import HonParameterFixed
from .parameter.program import HonParameterProgram


class HonCommandLoader:
    """Loads and parses the hOn command data."""

    def __init__(self, api: Any, appliance: Any) -> None:
        self._api = api
        self._appliance = appliance
        self._api_commands: dict[str, Any] = {}
        self._favourites: list[dict[str, Any]] = []
        self._command_history: list[dict[str, Any]] = []
        self._commands: dict[str, HonCommand] = {}
        self._command_payload: dict[str, str] = {}
        self._appliance_data: dict[str, Any] = {}
        self._additional_data: dict[str, Any] = {}

    @property
    def api(self) -> Any:
        if self._api is None:
            raise NoAuthenticationException("Missing hOn login")
        return self._api

    @property
    def appliance(self) -> Any:
        return self._appliance

    @property
    def commands(self) -> dict[str, HonCommand]:
        return self._commands

    @property
    def appliance_data(self) -> dict[str, Any]:
        return self._appliance_data

    @property
    def additional_data(self) -> dict[str, Any]:
        return self._additional_data

    @property
    def command_payload(self) -> dict[str, str]:
        """Top-level key of the commands payload -> what became of it.

        "command" (parsed, in `commands`), "additional_data" (a non-dict value, kept
        aside) or "unparsed" (a dict that matched neither shape and was dropped). It
        answers a question no other dump section can: whether the appliance advertises a
        command this integration never sees.
        """
        return self._command_payload

    @property
    def command_history(self) -> list[dict[str, Any]]:
        """The appliance's accepted-command history, as the cloud returns it.

        Already fetched to recover the last command state; kept addressable so the
        appliance can hold on to it. It is the only record of the ENVELOPES the official
        app sends -- commandName, programName and the parameters, including the
        `operationName` that decides which operation a `settings` write performs -- which
        is what the diagnostics dump needs to answer "what else does this command accept".
        """
        return self._command_history

    async def load_commands(self) -> None:
        await self._load_data()
        self._appliance_data = self._api_commands.pop("applianceModel", {})
        self._get_commands()
        self._add_favourites()
        self._recover_last_command_states()

    async def _load_commands(self) -> None:
        self._api_commands = await self._api.load_commands(self._appliance)

    async def _load_favourites(self) -> None:
        self._favourites = await self._api.load_favourites(self._appliance)

    async def _load_command_history(self) -> None:
        self._command_history = await self._api.load_command_history(self._appliance)

    async def _load_data(self) -> None:
        await asyncio.gather(
            self._load_commands(),
            self._load_favourites(),
            self._load_command_history(),
        )

    @staticmethod
    def _is_command(data: dict[str, Any]) -> bool:
        return (
            data.get("description") is not None and data.get("protocolType") is not None
        )

    @staticmethod
    def _clean_name(category: str) -> str:
        if "PROGRAM" in category:
            return category.split(".")[-1].lower()
        return category

    def _get_commands(self) -> None:
        commands = []
        self._command_payload = {}
        for name, data in self._api_commands.items():
            command = self._parse_command(data, name)
            if command is not None:
                commands.append(command)
                self._command_payload[name] = "command"
            elif not isinstance(data, dict):
                self._command_payload[name] = "additional_data"
            else:
                # A dict that is neither a command nor a set of categories. Dropped
                # silently until now, which made it impossible to tell "the appliance
                # offers nothing else" from "we failed to parse what it offers".
                self._command_payload[name] = "unparsed"
        self._commands = {c.name: c for c in commands}

    def _parse_command(
        self,
        data: dict[str, Any] | str,
        command_name: str,
        categories: Optional[dict[str, HonCommand]] = None,
        category_name: str = "",
    ) -> Optional[HonCommand]:
        if not isinstance(data, dict):
            self._additional_data[command_name] = data
            return None
        if self._is_command(data):
            return HonCommand(
                command_name,
                data,
                self._appliance,
                category_name=category_name,
                categories=categories,
            )
        if category := self._parse_categories(data, command_name):
            return category
        return None

    def _parse_categories(
        self, data: dict[str, Any], command_name: str
    ) -> Optional[HonCommand]:
        categories: dict[str, HonCommand] = {}
        for category, value in data.items():
            if command := self._parse_command(
                value, command_name, category_name=category, categories=categories
            ):
                categories[self._clean_name(category)] = command
        if categories:
            # setParameters must come first
            if "setParameters" in categories:
                return categories["setParameters"]
            return list(categories.values())[0]
        return None

    def _get_last_command_index(self, name: str) -> Optional[int]:
        return next(
            (
                index
                for (index, d) in enumerate(self._command_history)
                if d.get("command", {}).get("commandName") == name
            ),
            None,
        )

    def _set_last_category(
        self,
        command: HonCommand,
        name: str,
        parameters: dict[str, Any],
        program_name: str = "",
    ) -> HonCommand:
        """Point `name` at the category the last accepted command used.

        The swap is applied to the LOADER's own dict rather than through the
        ``command.category`` setter. That setter writes into
        ``appliance.commands[name]``, but during a load the appliance has not adopted
        this loader's dict yet -- ``HonAppliance.load_commands`` assigns
        ``self._commands = command_loader.commands`` only AFTER we return, which would
        overwrite the swapped entry and silently discard the recovery. Writing here is
        what makes ``return self._commands[name]`` (this method's stated intent) true.
        """
        if not command.categories:
            return command
        if program := parameters.pop("program", None):
            category = self._clean_name(str(program))
        elif (category := parameters.pop("category", None)) is not None:
            category = str(category)
        elif program_name:
            # The command's own programName, when the payload names no category at all.
            # For a category-split startProgram the program is carried BY the category
            # (api.send_command derives programName from the active category's name), so
            # such a payload legitimately has no `program` parameter -- live-observed on a
            # heat pump water heater, whose accepted commands read
            # {machMode, onOffStatus, tempSel} next to programName "PROGRAMS.HW.AUTO".
            # Without this the recovery gave up and the schema's FIRST category stayed
            # active, so the next write would have re-labelled the appliance with a
            # program it was not running.
            category = self._clean_name(str(program_name))
        else:
            return command
        # Same guard as the category setter: an unknown category leaves the default in
        # place instead of raising on a stale/renamed program in the history.
        if category in command.categories:
            selected = command.categories[category]
            # This swap bypasses the `category` setter (see the docstring), so the
            # "deliberately selected" mark has to be applied here too -- otherwise a
            # recovered program would be indistinguishable from the schema default.
            selected.mark_selected_explicitly()
            self._commands[name] = selected
        return self._commands[name]

    def _recover_last_command_states(self) -> None:
        for name, command in self.commands.items():
            if (last_index := self._get_last_command_index(name)) is None:
                continue
            last_command = self._command_history[last_index]
            last = last_command.get("command", {})
            parameters = last.get("parameters", {})
            command = self._set_last_category(
                command, name, parameters, str(last.get("programName") or "")
            )
            for key, data in command.settings.items():
                if parameters.get(key) is None:
                    continue
                with suppress(ValueError):
                    data.value = parameters.get(key)

    def _add_favourites(self) -> None:
        for favourite in self._favourites:
            name, command_name, base = self._get_favourite_info(favourite)
            if not base:
                continue
            base_command: HonCommand = copy(base)
            self._update_base_command_with_data(base_command, favourite)
            self._update_base_command_with_favourite(base_command)
            self._update_program_categories(command_name, name, base_command)

    def _get_favourite_info(
        self, favourite: dict[str, Any]
    ) -> tuple[str, str, HonCommand | None]:
        name = str(favourite.get("favouriteName", ""))
        command = favourite.get("command", {})
        if not isinstance(command, dict):
            return name, "", None
        command_name = str(command.get("commandName", ""))
        if not command_name:
            return name, "", None
        parent = self.commands.get(command_name)
        if parent is None:  # stale favourite: command no longer available
            return name, command_name, None
        program_name = self._clean_name(str(command.get("programName", "")))
        base_command = parent.categories.get(program_name)
        return name, command_name, base_command

    def _update_base_command_with_data(
        self, base_command: HonCommand, command: dict[str, Any]
    ) -> None:
        for data in command.values():
            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if not (parameter := base_command.parameters.get(key)):
                    continue
                with suppress(ValueError):
                    parameter.value = value

    def _update_base_command_with_favourite(self, base_command: HonCommand) -> None:
        extra_param = HonParameterFixed("favourite", {"fixedValue": "1"}, "custom")
        base_command.parameters.update(favourite=extra_param)

    def _update_program_categories(
        self, command_name: str, name: str, base_command: HonCommand
    ) -> None:
        program = base_command.parameters["program"]
        if isinstance(program, HonParameterProgram):
            program.set_value(name)
        self.commands[command_name].categories[name] = base_command
