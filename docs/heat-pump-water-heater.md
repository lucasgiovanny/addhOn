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

`settings` is split into two categories, and only the first is useful: `setParameters`
holds the 43 parameters above, `setConfig` holds `httpEndpoint` and `mqttEndpoint`
(cloud plumbing). So there is no second operation hiding behind the category selector —
confirmed on the 2026-08-25 dump.

`startProgram` is split into one category **per program** (`auto` / `eco` / `elec` /
`vac`), and this is load-bearing: the program travels as the command's `programName`,
derived from the active category, **not** as a payload parameter. The appliance's own
accepted commands read `{machMode, onOffStatus, tempSel}` next to
`programName: "PROGRAMS.HW.AUTO"`. Two consequences, both fixed in v5.23.1:

- every setpoint or power write re-asserts the program the appliance *reports*, so the
  envelope can never tell a unit running `eco` to start `auto`;
- the command loader now recovers the active category from `programName` when the
  payload names none, instead of leaving the schema's first category selected.

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

**Why read-only, and what would change it.** Nothing about the Home Assistant side stops
these from being editable — a time picker per window is a small amount of code. What
stops it is that the write would not reach the appliance: it would arrive labelled
`grSetVacDate` and be dropped. The moment the `operationName` the app uses for a given
group is known, that group becomes writable.

That name cannot be read anywhere. Five captures spanning a month confirm it: the command
definition always advertises `grSetVacDate` (it did not budge when the anti-legionella
hour was changed from the app and reached the appliance), the shadow mirrors the same
value, and the command history records **program starts only** — five entries, every one
a `startProgram`. So the remaining route is to try candidates, which is what
`addhon.send_command` is for (see below).

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

## Trying an operation name: `addhon.send_command`

A diagnostic service that sends **one raw command to one appliance**, deliberately
ungated — the one place in the integration that is. Everything else refuses a write the
appliance would swallow; this exists precisely to make that write and see what happens.

```yaml
action: addhon.send_command
target:
  device_id: <your water heater>
data:
  command: settings
  parameters:
    operationName: grSetSterilization   # the candidate under test
    sterilizationTime: "12:00"
```

Then check the entity (or the next diagnostics dump): if `sterilizationTime` moved, the
name is right. A wrong name is the same no-op the appliance already performs today, so
the cost of a miss is nothing. Parameter values still go through the engine's own
setters, so a range or enum parameter rejects an out-of-schema value exactly as it would
from an entity.

Naming pattern, from the one confirmed operation: `grSet` + the thing it sets
(`grSetVacDate`). Plausible siblings to try: `grSetSterilization`,
`grSetSterilizationTime`, `grSetTiming`, `grSetTimingPower`, `grSetOppEco`,
`grSetSilent`, `grSetOnOff`.

**Every name that works belongs in `SETTINGS_OPERATION_PARAMS` in `hon_commands.py`**,
mapped to the parameters it carries. That is what turns the matching controls from
read-only sensors into real entities.

## Reporting a new model

`custom_components/addhon`'s diagnostics dump is the input for everything above.

Two sections exist specifically to answer the one question still open — **which other
operations does `settings` accept?**

- `command_categories` (v5.22.0): the command categories the active one hides. On this
  appliance it answered one question and closed it — `settings`' hidden `setConfig`
  category is only cloud endpoints, so the other operations are not there.
- `command_history` (v5.23.0): the envelopes actually **sent** to the appliance, with a
  `source` field saying whether each came from the hOn app or from this integration.

On the HP250M7C-F9 both came back negative, which is itself the finding: the hidden
`setConfig` category holds only cloud endpoints, and the history records **program starts
only** (five `startProgram` entries, no `settings` command from the app even after one
was demonstrably performed). The operation names are not readable — they have to be
tried, with `addhon.send_command` above.

On another model either section may well answer directly, which is why both are dumped.
