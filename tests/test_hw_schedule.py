# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the heat pump water heater's schedule derivations (hw_values).

Every expected value below comes from the four HP250M7C-F9 diagnostics dumps taken
between 2026-07-26 and 2026-08-25: the appliance schedules itself with a daily power
timer (timingOnOffStatus + timingPowerOn/Off), up to three "cheap energy" windows per
period group (opp{1,2}Eco{Start,End}Time{1,2,3}) with a day mask, and two quiet windows
(silent{Start,End}Time{1,2}). All of them read 00:00 on every capture -- the features are
off on this unit -- which is exactly why the "unused slot" rule is load-bearing: without
it every one of these sensors would report a zero-length window as if it were configured.

`weekDay` is verified against the appliance's OWN `date` field on all four captures; it
is what makes the 7-slot Day energy series addressable.

The module is loaded straight from its file rather than through the package, which also
pins the claim its docstring makes: hw_values imports no Home Assistant at all.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "addhon"
    / "hw_values.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("addhon_hw_values_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hw = _load()


def _reader(attributes: dict):
    return lambda key: attributes.get(key)


class TimeTest(unittest.TestCase):
    def test_normalises_a_device_clock_field(self) -> None:
        self.assertEqual(hw.hw_time("22:00"), "22:00")
        self.assertEqual(hw.hw_time("9:05"), "09:05")
        self.assertEqual(hw.hw_time(" 00:00 "), "00:00")

    def test_absent_is_none(self) -> None:
        self.assertIsNone(hw.hw_time(None))

    def test_the_command_side_placeholder_is_refused(self) -> None:
        # The settings COMMAND declares timingPowerOn as a range[0,1] while the shadow
        # reports "00:00"; a bare 0 must never be rendered as a time.
        self.assertIsNone(hw.hw_time(0))
        self.assertIsNone(hw.hw_time("1"))

    def test_out_of_range_is_refused(self) -> None:
        self.assertIsNone(hw.hw_time("24:00"))
        self.assertIsNone(hw.hw_time("12:60"))
        self.assertIsNone(hw.hw_time("ab:cd"))


class WindowTest(unittest.TestCase):
    def test_configured_window(self) -> None:
        self.assertEqual(hw.hw_window("11:00", "16:00"), "11:00-16:00")

    def test_unused_slot_is_none(self) -> None:
        # How the appliance spells an empty slot on every capture.
        self.assertIsNone(hw.hw_window("00:00", "00:00"))

    def test_any_equal_pair_is_not_a_window(self) -> None:
        self.assertIsNone(hw.hw_window("07:30", "07:30"))

    def test_half_a_window_is_not_a_window(self) -> None:
        self.assertIsNone(hw.hw_window("11:00", None))
        self.assertIsNone(hw.hw_window(None, "16:00"))


class EcoScheduleTest(unittest.TestCase):
    def test_the_real_dump_reports_nothing_scheduled(self) -> None:
        attributes = {
            f"opp{group}Eco{edge}Time{slot}": "00:00"
            for group in (1, 2)
            for edge in ("Start", "End")
            for slot in (1, 2, 3)
        }
        self.assertIsNone(hw.hw_eco_schedule(_reader(attributes), 1))
        self.assertIsNone(hw.hw_eco_schedule(_reader(attributes), 2))

    def test_configured_windows_are_joined_in_slot_order(self) -> None:
        attributes = {
            "opp1EcoStartTime1": "11:00", "opp1EcoEndTime1": "16:00",
            "opp1EcoStartTime2": "00:00", "opp1EcoEndTime2": "00:00",
            "opp1EcoStartTime3": "20:00", "opp1EcoEndTime3": "22:30",
        }
        self.assertEqual(
            hw.hw_eco_schedule(_reader(attributes), 1), "11:00-16:00, 20:00-22:30"
        )

    def test_the_two_groups_are_independent(self) -> None:
        attributes = {
            "opp1EcoStartTime1": "11:00", "opp1EcoEndTime1": "16:00",
            "opp2EcoStartTime1": "01:00", "opp2EcoEndTime1": "05:00",
        }
        self.assertEqual(hw.hw_eco_schedule(_reader(attributes), 1), "11:00-16:00")
        self.assertEqual(hw.hw_eco_schedule(_reader(attributes), 2), "01:00-05:00")


class SilentScheduleTest(unittest.TestCase):
    def test_unset_is_none(self) -> None:
        attributes = {
            f"silent{edge}Time{slot}": "00:00"
            for edge in ("Start", "End")
            for slot in (1, 2)
        }
        self.assertIsNone(hw.hw_silent_schedule(_reader(attributes)))

    def test_both_slots_are_reported(self) -> None:
        attributes = {
            "silentStartTime1": "23:00", "silentEndTime1": "07:00",
            "silentStartTime2": "13:00", "silentEndTime2": "15:00",
        }
        self.assertEqual(
            hw.hw_silent_schedule(_reader(attributes)), "23:00-07:00, 13:00-15:00"
        )


class WeekdayTest(unittest.TestCase):
    """The four captures, verbatim: the device's weekDay is the ISO weekday of its
    own `date`. That is what index weekDay - 1 into the 7-slot Day series relies on."""

    CAPTURES = (
        ("2026-07-26", 7),
        ("2026-08-16", 7),
        ("2026-08-18", 2),
        ("2026-08-25", 2),
    )

    def test_matches_the_appliance_date_on_every_capture(self) -> None:
        for date_value, weekday in self.CAPTURES:
            with self.subTest(date=date_value):
                reader = _reader({"date": date_value, "weekDay": weekday})
                self.assertEqual(hw.hw_weekday(reader), weekday)

    def test_falls_back_to_the_appliance_date(self) -> None:
        for date_value, weekday in self.CAPTURES:
            with self.subTest(date=date_value):
                self.assertEqual(hw.hw_weekday(_reader({"date": date_value})), weekday)

    def test_an_out_of_range_weekday_falls_back_rather_than_indexing_blindly(self) -> None:
        reader = _reader({"weekDay": 0, "date": "2026-08-25"})
        self.assertEqual(hw.hw_weekday(reader), 2)

    def test_no_clock_at_all_is_none(self) -> None:
        self.assertIsNone(hw.hw_weekday(_reader({})))
        self.assertIsNone(hw.hw_weekday(_reader({"date": "not-a-date"})))


class HaFreeModuleTest(unittest.TestCase):
    def test_the_module_imports_no_home_assistant(self) -> None:
        # The module docstring promises it: every platform, and this test file, import it
        # without stubbing the entity stack. Checked on the IMPORT statements rather than
        # on the text -- the comments legitimately name homeassistant.const.
        import ast

        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(
            {name for name in imported if name.split(".")[0] == "homeassistant"},
            f"hw_values must stay Home Assistant-free, imports: {sorted(imported)}",
        )


if __name__ == "__main__":
    unittest.main()
