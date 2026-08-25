# Heat pump water heater (`HW`)

Everything below is ground-truthed on a real **Haier HP250M7C-F9** (250 L), from four
diagnostics dumps taken between 2026-07-26 and 2026-08-25. Where a claim rests on a
single observation it says so.

## The one thing to know: `settings` is pinned to one operation

The appliance exposes exactly two commands, and they are not equal.

| Command | What it can do |
|---|---|
| `startProgram` | `program` (auto / eco / elec / vac), `tempSel`, `onOffStatus`, `machMode` |
| `settings` | everything else — **but only for one operation at a time** |

`settings` declares `operationName` as a **mandatory `fixed` parameter offering a single
value**, and on this model that value is `grSetVacDate` on every capture, a month apart.
Since `command.send()` transmits the whole parameter group, *every* write through
`settings` reaches the appliance labelled "set the vacation dates" — and the appliance
acts on the dates and silently drops the rest. The cloud accepts the command either way,
so nothing looks like an error.

That is why:

- the **target temperature** is written through `startProgram` (fixed in v5.10.0 after
  the setpoint kept reverting on the next poll);
- **power** is written through `startProgram` too (fixed in v5.22.0 — before that `off`
  did nothing at all, and `onOffStatus` stayed `1` across all four dumps);
- the **vacation window** works, because it *is* the pinned operation;
- the remaining `settings` toggles and setpoints (boost, silent, child lock,
  anti-legionella enable/temperature, the PV/SG/HC setpoints) **read** correctly but
  refuse to write, with a clear error naming the operation that would swallow them.
  Change those in the hOn app.

If your appliance reports a different `operationName`, or none, the integration does not
gate anything: an operation it has not ground-truthed is never treated as blocking.

## Running the appliance on solar

Three ways, best first.

### 1. The appliance's own schedule (no Home Assistant involved)

The unit has a full scheduler that nothing in Home Assistant could see before v5.22.0:

Entities are listed by their displayed name; the entity id follows your Home Assistant
language, so look them up on the device page rather than typing them from here.

| What | Attributes | Entities |
|---|---|---|
| Daily power timer | `timingOnOffStatus`, `timingPowerOn`, `timingPowerOff` | *Timer*, *Timer switch-on*, *Timer switch-off* |
| Off-peak / "cheap energy" windows — up to **3 per group, 2 groups** | `opp{1,2}Eco{Start,End}Time{1,2,3}`, `opp1EcoDays` | *Off-peak windows 1* / *2*, *Off-peak days* |
| Quiet windows — up to 2 | `silent{Start,End}Time{1,2}` | *Silent windows* |
| Anti-legionella | `sterilizationTime`, `sterilizationInterval` | *Anti-legionella time*, *Anti-legionella interval* |

Some of these register disabled by default (the second window group, the day mask, the
quiet windows, the interval); enable them from the entity registry if you use them.

Set the off-peak windows in the hOn app to your solar hours and the appliance does the
rest — no cloud round-trip, no automation, and it keeps working when Home Assistant is
down. The integration mirrors the configuration read-only, for the reason above.

`opp1EcoDays` is reported as a hex bitmask (`7F` = all seven days) and is exposed
**raw**: only the all-days value has ever been observed, so which bit is which weekday
is not knowable from the evidence and is not guessed.

### 2. The photovoltaic / off-peak dry contact

The appliance has a hardware input with its own setpoints — this is the proper
solar-priority mechanism:

| Attribute | Meaning | Entity |
|---|---|---|
| `offpeakSignalSwitch` | is the input enabled at all | *Off-peak input* |
| `offpeakSignalCurrentStatus` | is the contact closed right now | *Off-peak signal* |
| `offpeakSignalNcNo` | normally-closed / normally-open | — |
| `offpeakSignalHeatMode`, `offpeakSignalHeatStrategy` | how it heats while the signal is on | *Off-peak heating mode* / *strategy* (the HP250M7C-F9 does not report the strategy in its shadow, so that one does not appear on this model) |
| `powerSupplySource` | which auxiliary source is selected | *Power supply source* |
| `tempSelPv` / `tempSelSg` / `tempSelHc` | the target used in PV / smart-grid / heater+compressor mode | *Target temperature (PV mode)* / *(smart grid)* / *(heater + compressor)* |

All of these register disabled by default.

Wire a relay from the inverter (or from Home Assistant) to the input, enable it in the
app, and the tank charges to `tempSelPv` only while there is surplus. The value
semantics of the mode/strategy numbers are **not** ground-truthed — every capture reads
the factory default — so they are exposed as raw figures rather than labelled options.

### 3. A Home Assistant automation on the setpoint

The `water_heater` entity writes through `startProgram`, the one channel proven to work
on this appliance. Drive the **setpoint**, not `off`: the tank is a thermal battery, and
a low setpoint fails safe (lukewarm water) where a failed `off` fails cold and skips the
anti-legionella cycle.

```yaml
automation:
  - alias: Water heater - solar charge
    triggers:
      - trigger: numeric_state
        entity_id: sensor.solar_surplus_power
        above: 800
        for: "00:10:00"
    conditions:
      - condition: sun
        after: sunrise
        before: sunset
    actions:
      - action: water_heater.set_operation_mode
        target: { entity_id: water_heater.your_device }
        data: { operation_mode: eco }        # eco = heat pump only
      - action: water_heater.set_temperature
        target: { entity_id: water_heater.your_device }
        data: { temperature: 62 }

  - alias: Water heater - off sun
    triggers:
      - trigger: numeric_state
        entity_id: sensor.solar_surplus_power
        below: 200
        for: "00:20:00"
      - trigger: sun
        event: sunset
        offset: "-00:30:00"
    actions:
      - action: water_heater.set_temperature
        target: { entity_id: water_heater.your_device }
        data: { temperature: 42 }
```

Operation modes in Home Assistant are `heat_pump` (the device's `auto`), `eco`,
`electric` (`elec`), `vac` and `off`. Prefer `eco` even in full sun: the compressor puts
3-4x more energy into the tank per kWh drawn than the resistance does. Every
`set_temperature` re-sends `startProgram`, so debounce the triggers (as above) instead of
following surplus minute by minute.

## Energy counters

The counters are `;`-separated period series, not scalars:

| Series | Slots | Index |
|---|---|---|
| `energyConsumptionDay*`, `accumulatedHeatDay` | 7 | `weekDay - 1` (ISO: Mon = 1 … Sun = 7) |
| `energyConsumptionMonth*`, `accumulatedHeatMonth` | 12 | calendar month of the device's own `date` |
| `energyConsumptionYear*`, `accumulatedHeatYear` | 5 | last element = current year |

`Cp` is the compressor, `Ec` the electric backup heater. *Total energy used* sums both
across the whole Year window and is the one to point the Energy dashboard at.

The invariant the month/year sensors rest on — the 12 month slots sum to the Year
series' last element — holds on every capture (2026-08-25: Cp 45, Ec 11, heat 314).

The daily sensors are **disabled by default**: the counters are whole kWh device-side
and this appliance burns about 1 kWh a day, so a daily electricity slot only ever reads
0, 1 or 2. The daily *heat output* carries more detail and is worth enabling.

`accumulatedHeat*` deliberately has **no** energy device class. Against the electricity
counters it implies a COP of 5-6 (August: 147 against 29 kWh), which is above the range
a heat pump water heater is specified for, so either the counter is not kWh or the
integer-truncated inputs understate consumption. Until that is settled it is not offered
to the Energy dashboard, where adding it would inflate the household total.

## Reporting a new model

`custom_components/addhon`'s diagnostics dump is the input for everything above. Since
v5.22.0 it also carries `command_categories` — the command categories the active one
hides. On this appliance `settings` enumerates both `setConfig` and `setParameters`
while only one is loaded, so that section is where the still-unanswered question lives:
**which other operations does `settings` accept?** A dump taken right after using the
feature in the hOn app also records the app's own command envelope under
`attributes.commandHistory`, which is the cleanest way to learn an `operationName`.
