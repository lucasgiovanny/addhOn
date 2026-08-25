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
value** — `grSetVacDate` on every capture, a month apart — and `command.send()` transmits
the whole parameter group, so every write reaches the appliance under that label. The
cloud accepts it either way, so a dropped write never looks like an error.

**What actually decides is the `mandatory` flag, not the operation name.** Six live data
points, without exception:

| parameter | `mandatory` | result |
|---|---|---|
| `vacStartDate` / `vacEndDate` | 1 | written OK (v5.19.0) |
| `opp1EcoStartTime1` / `EndTime1` | 1 | written OK — 11:00-16:00, 2026-08-25 |
| `tempSel` | 0 | dropped (v5.10.0, live-verified) |
| `onOffStatus` | 0 | dropped (v5.22.0, live-verified) |
| `sterilizationTime` | 0 | dropped (probed twice, 2026-08-25) |

A pinned settings command writes its **mandatory group**. On this appliance that group is
exactly the schedule subsystem — the holiday window, the off-peak windows of both period
groups, their day mask, the quiet windows and the daily power timer — and none of the
temperature, boost, quiet or anti-legionella toggles, which are the ones that never
worked. Overriding `operationName` changed nothing in either direction: the name is a red
herring.

That is why:

- the **target temperature** is written through `startProgram` (fixed in v5.10.0 after
  the setpoint kept reverting on the next poll);
- **power** is written through `startProgram` too (fixed in v5.22.0 — before that `off`
  did nothing at all, and `onOffStatus` stayed `1` across four dumps). **Confirmed live**
  on 2026-08-25: a `startProgram` carrying `onOffStatus: "0"` was accepted and the
  appliance reported itself off;
- the **vacation window** works, because it *is* the pinned operation;
- the **off-peak and quiet windows are writable** as `time` entities (v5.26.0), because
  they are mandatory;
- the remaining `settings` toggles and setpoints (boost, silent, child lock,
  anti-legionella enable/temperature, the PV/SG/HC setpoints) are all mandatory 0: they
  **read** correctly but refuse to write, with a clear error. Change those in the hOn app.

One more consequence, fixed in v5.26.0: because a command sends its whole parameter
group, a write also re-sends the fields it is not changing. The cloud mistypes three of
them — `timingPowerOn` / `timingPowerOff` as `range[0,1]` and `opp1EcoDays` as
`range[0,40]`, while the appliance reports `"00:00"` and `"7F"` — so the parameter could
never hold its own value and every settings write sent `0` instead, wiping a timer or a
day mask set in the app. Those three are now transmitted at the appliance's own reading.

If your appliance reports a different `operationName`, or none, the integration does not
gate anything: an operation it has not ground-truthed is never treated as blocking.

## Running the appliance on solar

Three ways, best first.

### 1. The appliance's own schedule — now editable from Home Assistant

Set *Off-peak window 1 start* and *end* to your solar hours and the appliance does the
rest: no cloud round-trip at run time, no automation, and it keeps working when Home
Assistant is down. This is the best of the three.

The three off-peak windows of period group 1 and the two quiet windows are `time`
entities; only the first off-peak pair is enabled by default. A slot with both ends at
the same time is unused — that is how the appliance spells an empty slot.

Two parts stay read-only, and not for want of trying: the daily power timer and the day
mask are mandatory too, but the cloud declares them as numeric ranges while the appliance
reports a clock reading and a hex mask, so a time cannot be assigned to those parameters
at all. Set those two in the hOn app; everything else is here.

### 1b. The appliance's own schedule (details)

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

## The holiday window

The one part of the `settings` command that *is* writable, because it is the operation
the command is pinned to. Two `date` entities plus a button:

| Entity | What it does |
|---|---|
| *Vacation start* / *Vacation end* | the two halves of the window |
| *Clear vacation window* (button) | cancels it |

The appliance spells "no window" as **2000-01-01 on both halves** — that is what the
captures show once a window is cleared from the app, with `machMode` back on the normal
program. So:

- both dates report **no value** when they hold the sentinel, rather than a holiday in
  the year 2000;
- setting one half of a window that does not exist yet writes **both**, as a one-day
  window you then stretch. Without that the two entities dead-lock each other: the
  ordering guard refuses a start later than the (unset) end, so a window could only ever
  be started from its end date;
- the button writes the sentinel to both halves in one command, which is how the app
  cancels it. Home Assistant has no "empty" input for a `date` entity, so without the
  button the only way to cancel from Home Assistant would be to type 2000-01-01 by hand.

The window is scheduled BY DATES and is distinct from the `vac` program, which the
`water_heater` entity's away toggle starts immediately. The two compose: inside a
scheduled window the appliance runs the vac program by itself, which is why the entity
reports `vac` while the configured program stays put.

## Trying an operation name

Two services, both diagnostic and both deliberately ungated — the only places in the
integration that are. Everything else refuses a write the appliance would swallow; these
exist precisely to make that write and see what happens.

### `addhon.probe_settings_operation` — the automated sweep

A diagnostic service that sends **one raw command to one appliance**, deliberately
ungated — the one place in the integration that is. Everything else refuses a write the
appliance would swallow; this exists precisely to make that write and see what happens.

Give it a parameter and a value it does not currently hold. It sends each candidate
operation name in turn, waits for the appliance to apply and the shadow to catch up,
reads the parameter back, and stops at the first one that moved it. The result arrives as
a notification.

```yaml
action: addhon.probe_settings_operation
target:
  device_id: <your water heater>
data:
  parameter: sterilizationTime
  value: "07:00"
```

The candidate ladder is derived from the parameter name, most likely first:

| Rung | `vacStartDate` | `timingPowerOn` |
|---|---|---|
| the whole name | `grSetVacStartDate` | `grSetTimingPowerOn` |
| first + last word | **`grSetVacDate`** | `grSetTimingOn` |
| prefixes, longest first | `grSetVacStart`, `grSetVac` | `grSetTimingPower`, `grSetTiming` |

The second rung is what produces `grSetVacDate`, the one operation name ever confirmed —
which is the only evidence the pattern rests on, so pass your own list with `operations:`
when you have a better idea.

The probe refuses to start when the parameter is not mirrored in the shadow (the result
could not be observed) or already holds the target value (every candidate would look like
a hit).

### `addhon.send_command` — one candidate, by hand

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

A wrong name is the same no-op the appliance already performs today, so the cost of a
miss is nothing. Parameter values still go through the engine's own setters, so a range
or enum parameter rejects an out-of-schema value exactly as it would from an entity.

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
- `command_payload` (v5.25.0): every top-level key the commands endpoint returned and
  what became of it — `command`, `additional_data` or `unparsed`. It answers a question
  none of the others can: whether the appliance advertises a command this integration
  never sees.

On the HP250M7C-F9 both came back negative, which is itself the finding: the hidden
`setConfig` category holds only cloud endpoints, and the history records **program starts
only** (five `startProgram` entries, no `settings` command from the app even after one
was demonstrably performed). The operation names are not readable — they have to be
tried, with `addhon.send_command` above.

On another model either section may well answer directly, which is why both are dumped.
