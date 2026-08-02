# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard: icons.json stays structurally valid and in sync with the translations.

Home Assistant resolves per-state entity icons from `icons.json` (icon translations).
Nothing else in the suite reads that file, so a typo would silently render a blank icon
in the UI. This test reproduces the parts of hassfest's icon schema that matter here and
cross-checks the state keys against the translated state labels: a mode can never gain a
label without an icon (or an icon without a label).
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "addhon"
ICONS = COMPONENT / "icons.json"

# hassfest accepts "mdi:<slug>" (and custom icon packs); the integration uses mdi only.
_ICON_RE = re.compile(r"^mdi:[a-z0-9-]+$")
# Icon translation keys are slugs, like translation keys.
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")


def _icons() -> dict:
    return json.loads(ICONS.read_text(encoding="utf-8"))


def _translated(platform: str, key: str) -> dict:
    data = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    return data["entity"][platform][key]


def _translated_states(platform: str, key: str) -> set[str]:
    return set(_translated(platform, key).get("state", {}))


class IconsStructureTest(unittest.TestCase):
    def test_every_icon_value_is_a_valid_mdi_name(self) -> None:
        def walk(node, path=""):
            for key, value in node.items():
                where = f"{path}.{key}" if path else key
                if isinstance(value, dict):
                    walk(value, where)
                else:
                    self.assertRegex(
                        value, _ICON_RE, f"{where}: not a valid mdi icon name"
                    )

        walk(_icons())

    def test_every_key_is_a_slug(self) -> None:
        def walk(node, path=""):
            for key, value in node.items():
                where = f"{path}.{key}" if path else key
                self.assertRegex(key, _SLUG_RE, f"{where}: key is not a slug")
                if isinstance(value, dict):
                    walk(value, where)

        walk(_icons()["entity"])

    def test_entity_blocks_declare_a_default(self) -> None:
        # hassfest marks `default` REQUIRED for an entity entry and for every
        # state_attributes entry; a missing one fails CI, not just the UI.
        for platform, keys in _icons()["entity"].items():
            for key, block in keys.items():
                self.assertIn("default", block, f"entity.{platform}.{key}")
                for attr, attr_block in block.get("state_attributes", {}).items():
                    self.assertIn("default", attr_block, f"{platform}.{key}.{attr}")
                    self.assertIn("state", attr_block, f"{platform}.{key}.{attr}")


class SensorIconsTest(unittest.TestCase):
    """A sensor block in icons.json exists to give an ENUM sensor per-state icons.

    Only the JSON-vs-JSON half lives here: this module deliberately imports nothing from
    the component (see the module docstring), so it stays runnable on its own. The
    cross-checks against the sensor descriptions themselves -- that the state keys match
    the declared ENUM `options`, and that none of these sensors carries a static icon that
    would override the whole block -- live in test_entity_translation_keys.py, which
    already runs the platform tables under stubs.
    """

    def test_state_icons_match_the_translated_states(self) -> None:
        blocks = _icons()["entity"].get("sensor", {})
        self.assertTrue(blocks, "no entity.sensor icon block to check")
        for tk, block in blocks.items():
            self.assertEqual(
                set(block["state"]),
                _translated_states("sensor", tk),
                f"icon/label mismatch for the '{tk}' sensor",
            )


class WaterHeaterIconsTest(unittest.TestCase):
    """The boiler's operating modes are model-specific (auto/eco/elec/vac on the
    HP250M7C-F9) rather than HA's standard water_heater states, so the frontend has no
    built-in icon for them: they MUST come from icons.json, for both the entity state and
    the operation_mode attribute (the mode picker)."""

    def _block(self) -> dict:
        return _icons()["entity"]["water_heater"]["water_heater"]

    def test_state_icons_match_the_translated_states(self) -> None:
        self.assertEqual(set(self._block()["state"]), _translated_states("water_heater", "water_heater"))

    def test_operation_mode_attribute_matches_the_state_icons(self) -> None:
        block = self._block()
        self.assertEqual(
            block["state_attributes"]["operation_mode"]["state"], block["state"]
        )

    def test_attribute_icons_match_the_translated_attribute_values(self) -> None:
        # `action` and `heat_source` are scalar enums rendered in a tile card's state
        # content: every value that has a label must have an icon, and vice versa.
        translated = _translated("water_heater", "water_heater")["state_attributes"]
        for attr, block in self._block()["state_attributes"].items():
            if attr == "operation_mode":  # mirrors the entity state, checked above
                continue
            self.assertEqual(
                set(block["state"]),
                set(translated[attr]["state"]),
                f"icon/label mismatch for the '{attr}' attribute",
            )

    def test_entity_declares_no_static_icon(self) -> None:
        # A static _attr_icon on the entity would override the icon translations and
        # freeze the icon on one mode, which is exactly what this file exists to avoid.
        source = (COMPONENT / "water_heater.py").read_text(encoding="utf-8")
        self.assertNotIn("_attr_icon =", source)


if __name__ == "__main__":
    unittest.main()
