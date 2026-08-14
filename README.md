<div align="center">

# addhOn

### Add your Haier devices to your home automation system

**A custom Home Assistant integration for controlling Haier appliances via the hOn cloud API. It discovers your paired appliances, exposes them as Home Assistant entities, and routes control commands to the supported types**

[![Release](https://img.shields.io/github/v/release/tis24dev/addhOn?logo=github&label=release)](https://github.com/tis24dev/addhOn/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/tis24dev/addhOn/ci.yml?branch=dev&label=CI&logo=github)](https://github.com/tis24dev/addhOn/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?logo=homeassistant&logoColor=white)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5.svg?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Last commit](https://img.shields.io/github/last-commit/tis24dev/addhOn?logo=github)](https://github.com/tis24dev/addhOn/commits)
[![💖 Sponsor](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-pink?logo=github)](https://github.com/sponsors/tis24dev)

</div>

---

## Features

- **Automatic device discovery** — discovers all paired Haier devices in your account
- **Multiple device support** — AC units, washing machines, and other hOn-compatible appliances
- **Real-time status** — monitors device state, temperature, modes, and cycle progress
- **Command execution** — control HVAC modes, set temperatures, start/stop programs, and more
- **Asyncio optimization** — dedicated background loop for reliable API communication
- **Smart attribute mapping** — device-specific attribute keys extracted from real diagnostics
- **Full Lovelace support** — integrates seamlessly with Home Assistant UI and automations
- **Two-factor authentication (2FA)** — Two-factor authentication during the login process is supported
- **Multilingual integration** — Currently available in Italian and English, with additional languages available upon request.

## Installation

### Prerequisites

- Home Assistant 2024.12.0 or newer
- Haier hOn app account credentials

### Method 1: HACS

Add this repository to HACS as a custom integration and install from the UI.

1. Login in Home Assistant UI → Select HACS in the left
2. Tap the three dots (menu) in the upper-right corner
3. Select **Custom repositories**
4. Add the repo link: **https://github.com/tis24dev/addhOn/** and type: **Integration**
5. Save
6. Search for **addhOn** and select it
7. Click the button in the lower right corner to download, then restart
8. Go to Settings → Devices & Services → **Add Integration**
9. Search for **addhOn** and select it
10. Enter your Haier hOn account email and password and submit. The credentials
   are validated against the hOn cloud and stored in the config entry.

If your hOn session later expires, Home Assistant shows a **Reconfigure**
(re-authentication) prompt asking only for the password again; no need to remove
and re-add the integration.

### Options

Open the integration entry and choose **Configure** to toggle:

- **Enable debug logging** — verbose integration logs.
- **Enable MQTT realtime debug** — verbose logs for the live MQTT stream.

Both persist across restarts. The polling interval is fixed at 60 seconds.

These toggles are also exposed as switches on a dedicated **addhOn diagnostics**
device (Settings > Devices & Services > addhOn), alongside read-only diagnostics
and quick-action buttons (refresh now, reset debug). A ready-to-paste dashboard
card is in [`docs/debug-device.md`](docs/debug-device.md).

## Supported Devices

### Supported appliance types

Air conditioners (AC), washing machines (WM), tumble dryers (TD), washer-dryers
(WD), refrigerators and freezers (REF/FR/FRE), ovens (OV), dishwashers (DW), wine
coolers (WC), hobs (IH/HOB), hoods (HO), coffee machines/kettles (KT), water
heaters (WH), heat pump water heaters (HW) and robot vacuums (RVC). Air conditioners and laundry appliances have
full control; the other types are exposed mainly as read-only sensors, with a few
controls where they have been mapped.

Heat pump water heaters also get a native **`water_heater` entity** (target
temperature, operating mode, power and holiday/away mode in a single card). It is
capability-gated on the device's live schema, so a plain water heater that exposes a
writable setpoint gets one too. The individual `number` / `select` / `switch` controls
stay alongside it — they write the same parameters and cover what the `water_heater`
domain has no slot for (boost, silent mode, child lock, sterilization, per-mode
setpoints).

The target temperature is **snapped onto the device's own min/max/step grid** before it
is sent, on both the `water_heater` and the `climate` (AC) entity. Home Assistant does
not enforce the step: its temperature dial derives the next setpoint from the entity
state, so a device whose cloud shadow reports an off-grid value (a real HP250M7C-F9
reported `tempSel 59.2` on a `35-75 step 1` range) turned every `+`/`-` press into a
value the appliance refuses — `Command failed: Allowed: min 35 max 75 step 1 But was:
60.2` — and the setpoint never moved. The request is now clamped to the device's bounds
and rounded to the nearest value it accepts. This applies only where the device actually
declares a range: with no declared grid the value is sent unchanged, so a half degree is
never rounded away on a parameter the appliance would have taken.

The operating modes are reported under Home Assistant's **standard** `water_heater`
state names, because the frontend's mode picker resolves its icons from a fixed map of
those names: the device's `auto` is `heat_pump`, `elec` is `electric`, `eco` is already
standard. Holiday (`vac`) has no standard equivalent, so it keeps the device code and is
additionally exposed as the away-mode toggle.

What the appliance is *doing* is exposed two ways, from one shared derivation, because
neither surface alone covers both uses:

- **As entities** — `sensor.<device>_heating_status` (`off` / `idle` / `heating`) and
  `sensor.<device>_heat_source` (compressor, electric heater, auxiliary heater, boiler,
  or `multiple`). These are the graphable handles: they get their own history timeline
  and can sit next to the water heater in a `history-graph` card. Use these for
  dashboards and automations.
- **As attributes on the `water_heater` entity** — the same two readings plus
  `hot_water_level` (the same 0-100 % the sensor reports). A tile card's *state content*
  can only show attributes of the entity it is bound to, which is what these are for.
  `hot_water_level` is a plain number there — attributes carry no unit, so use the sensor
  entity where the `%` matters.

The `water_heater` domain has no equivalent of climate's `hvac_action`, so this can
never be part of the entity state. It also cannot appear in the water heater's own
history: the more-info chart is single-entity by construction and plots only the current
and target temperature — hence the sensors. The exact combination of simultaneously
running sources stays visible in the per-source `binary_sensor` entities.

For the **Energy dashboard**, pick `sensor.<device>_total_energy`: the device's lifetime
electricity (compressor + electric backup heater in one `total_increasing` kWh counter,
read from the appliance's own multi-year history, so it starts at the real total rather
than zero). The per-source month/year counters exist too but register disabled — enable
them from the entity registry when the split matters. Two caveats inherited from the
appliance: the counters are whole kWh device-side, so the dashboard's hourly attribution
is a staircase (roughly one 1 kWh step per day on a heat pump water heater), and the
cloud history is a 5-year window, so on a device older than that "lifetime" means the
last five years. The heat-output counters are thermal, not electricity — they carry no
energy device class on purpose, so the dashboard will not offer them and the total is
never inflated by them.

### Tested on real hardware

- **AC Unit:** Haier AS35PBPHRA-PRE
- **AC Unit:** Haier AD71S2SM3FA(H)
- **Washing Machine:** Haier HW80-B14959TU1IT
- **Washing Machine:** Candy TCA286TM5-S
- **Tumble Dryer:** Haier HD100-C367GU1-IT
- **Tumble Dryer:** Haier HD90-A3959 INT
- **Refrigerator:** Haier HDPW5620CNPK
- **Refrigerator:** HCW58F18EWMP
- **Oven:** HWO60SM5T5BH
- **Heat pump water heater:** Haier HP250M7C-F9

Other hOn-compatible Haier appliances should work; feel free to test and report.

## Troubleshooting

### Capture debug logs

1. **Enable** — open **addhOn diagnostics** (Settings → Devices & Services →
   addhOn) and turn on **Debug logging**. Add **MQTT realtime debug** only when
   investigating push/MQTT updates. *(Same as integration → Configure → Enable
   debug logging.)*
2. **Reproduce** — trigger the problem; press **Refresh now** to force an
   immediate poll for discovery/polling issues.
3. **Download** — Settings → System → Logs → **Download full log**, then attach
   the `home-assistant.log` to your GitHub issue.
4. **Disable** — press **Reset debug** on the same device (turns both toggles off
   and restores the default log levels), or just switch them off.

Both toggles persist across restarts. Details:
[`docs/discovery-debugging.md`](docs/discovery-debugging.md) and
[`docs/mqtt-realtime-logging.md`](docs/mqtt-realtime-logging.md).

### Authentication Failed

- Re-enter your Haier email and password via the integration's re-authentication prompt (or remove and re-add the integration)
- Verify the account is active in the Haier hOn app
- If 2FA is enabled, disable it temporarily for the integration account

### Device Not Discovered

- Ensure the device is paired in the Haier hOn app
- Check internet connectivity
- After pairing a new device in the app, reload the integration (or restart Home Assistant)

### HVAC Mode / Fan Mode Issues

The integration auto-detects the modes each AC supports. If your AC does not respond to a mode:
- Enable debug logging (see above); the climate entity logs its detected `hvac_modes` and `fan_modes` at startup
- Report the unsupported mode on GitHub issues

## Development Notes

### Architecture Decisions

- **Asyncio background loop** — the native client's operations run in a dedicated thread-safe event loop to avoid blocking Home Assistant
- **Command routing** — different device types expect different command structures; the integration detects and routes appropriately
- **Attribute extraction** — device-specific attributes are extracted from real device diagnostics, not hardcoded

### Extending the Integration

To add support for a new Haier device:

1. Pair it in the Haier hOn app
2. Enable debug logging and capture the device diagnostics (see [`docs/discovery-debugging.md`](docs/discovery-debugging.md))
3. Add or extend the relevant platform file (`sensor.py`, `binary_sensor.py`, `number.py`, `select.py`, `switch.py`, ...) and, if the device type needs it, its capability map
4. Test with your device, then open a pull request (or an issue with the captured diagnostics)

## Contributing

Issues and pull requests are welcome! Please include:
- Home Assistant version
- Device model number
- Steps to reproduce

## Attribution

addhOn's cloud client originated from [pyhOn](https://github.com/Andre0512/pyhOn)
(MIT © 2023 Andre Basche) and still contains portions derived from it, tracked
per-module by the independence harness in [`tests/independence/`](tests/independence/).
See [`NOTICE`](NOTICE) for the current derivation status and attribution.

## License

Copyright (C) 2026 tis24dev

Licensed under the [GNU Affero General Public License v3.0](LICENSE). You may use,
modify, and distribute this integration under the AGPL terms; if you run a modified
version as a network service, you must offer users the corresponding source.
Contributions back via pull request are welcome. Previously released under MIT, then
PolyForm Noncommercial.

Portions remain derived from pyhOn (MIT © 2023 Andre Basche); that attribution is
preserved in [`NOTICE`](NOTICE) and applies to those portions.

See <https://www.gnu.org/licenses/agpl-3.0> for the full terms.

## Support

- **Issues:** GitHub Issues
- **Discussions:** GitHub Discussions
- **Documentation:** see the [`docs/`](docs/) folder

---

Built with ❤️ for Home Assistant enthusiasts.
