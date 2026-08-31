# TrueTemp — technical reference

This is the detailed, developer-facing reference: the control algorithm, what
gets learned and how, and the project history. For an overview of what the
integration does and how to install it, see the main [README](../README.md)
(Swedish) or the [English README](../README.en.md).

---

A Home Assistant integration that works out what outdoor temperature to show
your heat pump, so the house actually lands on the temperature you asked for.

Most heat pumps run a weather curve: they read the outdoor temperature and pick
a flow temperature from it. That loop never sees whether the house is actually
warm enough. TrueTemp closes it — by learning, from your house, what
outdoor reading produces the indoor temperature you want.

**There is nothing to tune.** No gains, no coefficients, no thresholds. Setup
asks four questions, none of them technical.

---

## How it works

It learns one thing: *what offset holds this house at target?*

```
at −5 °C, this house needs −2.3 °C of spoofing to hold 21 °C
```

That is the whole model. An offset is accumulated by integral action and stored
per outdoor-temperature band, so the answer at −15 °C is learned separately from
the answer at +5 °C — which is how a non-linear heat curve gets handled without
anyone modelling the curve.

The published value is that offset plus a few bounded corrections:

```
compensated outdoor = real outdoor
                    + learned offset      what your house needs
                    − wind               optional
                    + sun                optional
                    + price              optional
```

### It learns something in both states

Integral action only works while its output is actually applied — you cannot
measure the response to an adjustment you are not making — so the offset above
stops accumulating the moment compensation is switched off. That would make an
off period dead time, which matters because compensation now ships off.

So the two states learn different halves of the same picture:

| | What is measured | Answers |
| --- | --- | --- |
| **Off** | Where the pump's own curve leaves the house, per band | *How wrong is my curve today?* |
| **On** | What offset holds the house at target, per band | *What correction fixes it?* |

The off-state table records an absolute settled indoor temperature rather than a
distance from target, so changing your mind about what temperature you want does
not invalidate it. Switching on converts it into a starting offset against
whatever the target is at that moment.

### It measures how long your house takes

An integral controller that ignores dead time oscillates, so TrueTemp
measures two response times from your own data by correlating what it applied
against what the house did:

| | What it is | Used for |
| --- | --- | --- |
| **Response time** | Commanded heat → measurable indoor change | How fast the controller is allowed to correct |
| **Wind-down time** | Pump backs off → house actually stops gaining | Half of it sets how early to coast before an expensive hour |

They are deliberately different numbers. A concrete slab keeps delivering heat
for hours after the pump stops, so wind-down is typically the longer of the two.
Price braking is timed off half of it rather than the whole thing, since what
costs money is the compressor still drawing power, not the slab coasting on
stored heat afterward — and the measured figure conflates both. Both are on
`sensor.<name>_status` in plain minutes, and they say whether they are measured
yet or still using the default for your emitter type.

### It knows when the pump has run out

Below some outdoor temperature the pump is flat out and more spoofing buys
nothing. Each band learns its own capacity ceiling, so the controller stops
pushing where pushing does not help, rather than winding up and dumping that
stored correction the moment the weather warms. The same ceiling approximates
where resistive backup heat cuts in.

---

## Setup

1. Add this repository to HACS as a custom repository (category: Integration).
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **TrueTemp**.

Four questions:

- **Indoor temperature sensor.** Pick a room you actually live in, away from
  direct sun and draughts. Everything is learned from this reading, so a
  cupboard or a basement will teach it the wrong house.
- **Outdoor temperature sensor.** A real sensor if you have one — usually more
  accurate than a weather service estimate.
- **The temperature you want.**
- **Radiators, underfloor, or both.** Only used for the first few days, until
  it has measured your actual response times.

The same setup screen also offers up to four more indoor sensors and how to
combine them — optional, and covered in
[Multiple indoor sensors](#multiple-indoor-sensors).

Then wire `sensor.<name>_compensated_outdoor_temperature` into your heat pump's
outdoor-temperature input — or let TrueTemp push it for you (below).

**Compensation starts off.** A fresh install computes its recommendation and
shows it on the main sensor without touching your heat pump, so you can see
what it would do before letting it do anything. Turn it on with the
TrueTemp switch (or by setting the climate entity to Auto) when you are
ready.

**The off period is not wasted.** While compensation is off your heat pump runs
its own weather curve untouched, so TrueTemp spends the time measuring
where that curve actually leaves the house — per outdoor temperature band, once
the house has settled. That is the honest "before" picture, and you can read it
straight off the sensor: *the raw curve holds this house at 19.4 °C when it is
around freezing, 1.6 °C below what you asked for.*

**Switching on spends what it measured.** Bands with a baseline observation
start from the offset that measurement implies instead of from zero, so a house
that sat off for a fortnight does not begin from a standing start. Bands it has
genuinely learned are never overwritten — a measured response to its own action
beats an inference every time.

The offset is still clamped to ±1 °C at first, widening to ±5 °C over about
three days. Being seeded does **not** buy authority: that ramp asks how much
evidence there is that *its own* adjustments work, which watching an untouched
curve cannot answer. So a seeded install starts from a much better guess and is
still just as cautious on day one.

### Optional extras

All under **Configure**, all skippable:

- **Extra indoor sensors** — up to four more (five total), combined with the
  primary using `indoor_aggregation`. See
  [Multiple indoor sensors](#multiple-indoor-sensors).
- **Sun and wind** — needs a weather entity. Each can be switched off
  separately, and both should be, unless your house actually meets the
  condition below. See [When to enable sun and wind](#when-to-enable-sun-and-wind).
- **Electricity price** — needs a price sensor with today's and tomorrow's
  hourly prices.
- **Where to send the value** — pick one of three output modes
  (`output_mode`): outdoor-sensor spoofing (a `number.*` entity, and/or an
  OhmOnWifi / Ohmigo device's own local API by hostname — both can be used at
  once), the pump's own heat-curve-offset input, or a room-sensor `climate`
  entity for pumps and TRVs that expose one. Exactly one mode is ever live —
  see `const.py`'s comment above `CONF_OUTPUT_MODE` for why they're
  mutually exclusive. If your heat pump has no way to accept a
  smart-controlled value at all, an
  [Ohm on WiFi Plus](https://www.ohmigo.io/product-page/ohm-on-wifi-plus)
  device gives it one — see the main README for why.
- **Detailed local logging** — one line per cycle to a file in your config
  folder. Nothing leaves your machine.

### When to enable sun and wind

Both are feedforward: they move the published temperature *before* the house has
drifted, on the assumption that a known disturbance is coming. That only helps
if the disturbance is real **at your indoor sensor**. The whole loop keys off
that one sensor, so "does the sun warm the house" is not the question — the
question is "does the sun warm the spot I am measuring".

**Sun — only if your indoor sensor is somewhere the sun reaches.**
If the sensor is in a basement, a north-facing room, an interior hallway or any
other spot the sun never touches, leave this off. Solar compensation backs the
pump off in anticipation of warming that your sensor will never record, so the
learner sees only a room that got colder and spends the rest of the day pushing
back against it. Sunshine also correlates with cold, clear weather, which makes
the mistake look like evidence: the sunniest hours are often the coldest.

This is not a small effect either way. In a house where the sensor is in the
sun's path the solar gain can dominate a winter afternoon; in a basement it is
indistinguishable from zero. If you are unsure, leave it off and watch whether
your indoor reading actually climbs on clear days before switching it on.

**Wind — only for an old, draughty house.**
Off by default, and that is the right answer for most houses. Enable it only if
you have an older building with genuine air leakage, the kind you can feel
getting colder on a windy day. A reasonably sealed house barely responds to
wind, and its effect is in any case hard to separate from plain cold, because
wind and low temperatures arrive together. Compensating for an effect that is
not there adds ripple and buys nothing.

**If you switch them off, nothing is lost.** The learner still finds the offset
your house needs; it just reacts after the drift starts instead of before it.
Any steady bias from a disturbance you are not feeding forward gets absorbed
into the learned offset within a day.

### Multiple indoor sensors

The primary indoor sensor picked in Setup stays required and is still the
zone's identity (its entity id is the config entry's `unique_id`) — that does
not change. Up to four more can be added, either during setup or later via
**Configure**, and `indoor_aggregation` decides how the extras and the
primary combine into the single `indoor_temp_c` every downstream calculation
sees:

- **Average** (default) — the plain mean of every sensor currently reporting.
  The primary is not weighted specially.
- **Lowest** — the minimum. The house is comfortable once the worst room is;
  the average room then settles above target, and the price-saving floor
  (`comfort_min_c`) ends up protecting the coldest room instead of the
  average one.

Only sensors currently reporting a usable number are aggregated, so losing
one out of three does not stop the house — it just aggregates over the
remaining two. If which sensors are contributing changes between cycles (one
drops out or comes back), that step in the aggregate is not a real
temperature change, so TrueTemp freezes learning and drops the response-lag
buffer for exactly one cycle rather than reading it as drift.

Three Repairs distinguish what's wrong, at different severities:

| Issue | Severity | Meaning |
| --- | --- | --- |
| `indoor_sensor_unavailable` | WARNING | None of the configured sensors are reporting a number. Self-heals once one does. |
| `indoor_sensor_partial` | WARNING | At least one sensor is reporting and at least one isn't. Self-heals. |
| `indoor_sensor_missing` | ERROR | A configured entity id no longer exists at all — renamed, or its integration removed. Does not self-heal; needs the zone's settings edited. |

All three respect the startup grace period, so a sensor that reads
unavailable in the first minute after a Home Assistant restart raises
nothing.

`sensor.<name>_status` gains `indoor_sensor_count` (usable/configured),
`indoor_sensor_readings` (entity id → °C) and `indoor_aggregation_mode`; the
card's "Indoor now" row shows the mode and count once more than one sensor is
configured, e.g. `20.4 °C (lowest of 3)`, with a popover listing every
contributing sensor's reading.

### Acting on the forecast

**Act on the forecast** is a third, separate switch on the same page, also off
by default. Sun and wind above answer "what is happening right now"; this one
answers "what is about to happen".

It reads the hourly forecast and, when a cold front or a rising wind is
coming, starts heating a little harder *before* it lands — the same thing
price pre-charging already does ahead of a spike, using the same measured rise
time, so the extra heat has actually arrived by the time it is needed rather
than still being on its way.

Three properties keep it from becoming another thing to tune:

- **No new gain.** The outdoor part is a straight level shift: 1 °C of
  anticipated drop asks for at most 1 °C of colder spoof. Wind reuses the same
  coefficient its steady-state term already uses.
- **One-sided.** It only ever asks for *more* heat. Easing off ahead of a
  forecast warm-up would trade comfort against a forecast that might be wrong,
  with no saving attached, so it is not done.
- **Self-cancelling.** The push is the difference between the forecast now and
  the forecast later, so it decays to zero as the change arrives. It is also
  measured against the forecast's *own* current value, never your outdoor
  sensor — a forecast that reads consistently colder than your wall sensor
  produces exactly zero rather than a permanent nudge.

Both pushes share one 3 °C budget, so a front that fires both of them at once
cannot stack into an overshoot. While the pre-ramp is acting, learning pauses
(`learning_paused_because` says so), because the house is deliberately being
held above target and the learner must not integrate that away.

Wind's share only counts if the wind term is switched on; the outdoor share
needs nothing but a weather entity. If the weather integration provides no
*hourly* forecast, the whole thing quietly contributes zero.

**Sun gets the opposite treatment**, not a share of the bucket above. Losing
sun is already fully covered reactively — the live sun term recomputes every
cycle, so there is nothing useful to pre-empt when solar gain is about to
drop. *Gaining* sun is the case worth acting on ahead of time, and in the
opposite direction: if the heating output isn't backed off before the sun
arrives, the heat already in the pipe stacks with the incoming solar gain and
overshoots comfort. So this piece backs off *before* a forecast rise in sun,
timed off the measured *fall* time rather than the rise time — the existing
heat surplus needs to have had time to dissipate before the sun lands, not
still be arriving. It has its own separate 3 °C budget (backing off too far is
a comfort-undershoot bet if the forecast is wrong, a different risk from the
overshoot hedge the outdoor/wind budget covers), decays to zero the same
self-cancelling way, and only counts if the sun term is switched on. While it
is acting, learning pauses the same way, because the house is deliberately
being held *below* target this time.

---

## Saving money on electricity

Optional, and off until you give it a price sensor. Two controls, both on the
device page:

**Price saving** — how much comfort to trade.

| | Drifts down by | Banks heat first |
| --- | --- | --- |
| Comfort first | 0.5 °C | no |
| Balanced | 1.5 °C | no |
| Savings first | 3.0 °C | yes |

**Cold caution** — how careful to be when it is properly cold. This one is
deliberately a manual choice: coasting down is cheap, but getting the
temperature back at −15 °C is slow and may run through resistive backup heat at
a terrible efficiency. Whether that trade is worth making is a question about
money versus comfort, not something that can be measured off a wall.

| | Stops saving below |
| --- | --- |
| Keep saving in the cold | −20 °C |
| Balanced | −10 °C |
| Comfort first in the cold | −5 °C |

On top of that, once a temperature band has been observed long enough to know
how fast it recovers, the drift is limited to what can actually be bought back.

There is one hard floor, **"never let it get colder than"** (default 18 °C). No
amount of saving crosses it.

Braking starts one wind-down time before an expensive hour and, on Savings
first, heat is banked one response time before it — because heat has to have
*arrived*, not merely be on its way.

---

## Vacation plans

A list of named, independently-enabled setback plans, each timed off the
house's own measured lag rather than a manual dial — the multi-plan
generalisation of the earlier single holiday scenario.

Each plan has a name, an `enabled` flag, a setback floor (`min_temp_c`) and
one of three recurrence shapes:

| Recurrence | Fields | Example |
| --- | --- | --- |
| `once` | `start_date`, `end_date` | A specific trip |
| `weekly` | `start_weekday`/`start_time`, `stop_weekday`/`stop_time` (Monday=0..Sunday=6) | "Every Fri 18:00 → Mon 07:00", a cabin weekend |
| `yearly` | `start_month`/`start_day`, `end_month`/`end_day` (year-independent) | "Jul 1 → Jul 14"; wraps correctly over New Year's |

The plan list lives in the config entry's **options** (`vacation_plans`),
not in a separate store — unlike the old per-field `RestoreEntity` holiday
entities, an options-flow save no longer discards it, since the whole list
is one option value that survives the reload.

**Resolution is priority by list order, not lowest-temperature-wins.** Each
cycle, `vacation.resolve_vacation()` walks the plans in order and the first
one currently actively sagging the house (setback or ramping) wins outright.
A silent "whichever plan is colder" tiebreak was deliberately rejected —
the wrong failure mode for a heating setback if the user's actual intent
was the other plan. If two *enabled* plans' windows would ever overlap, the
save is refused (not just warned) rather than leaving an ambiguous list —
enforced identically by the options-flow steps and by the `vacation_plan_set`
service below.

A single master switch, `switch.<name>_vacation_mode`, suspends every plan
at once regardless of their individual `enabled` flags — "I'm home early"
never requires editing N records. It defaults to **on**; with no plans
configured yet, an armed-but-empty switch is simply a no-op, so a fresh
install starts ready rather than needing a first-run flip.

Once a plan's concrete `(start_at, return_at)` occurrence is picked, the
setback/ramp/return behaviour inside it is unchanged from the original
single-scenario design: the target drops to the plan's floor in one sharp
step at the start, the ramp back up is paced off the house's own measured
response time (not a fixed duration), and the house is back at the normal
target by 15:00 local on the end date — starting the ramp immediately at
the leave date instead if there isn't enough time for a gentle one. The
setback floor is clamped to a fixed 5 °C frost-safety minimum, deliberately
independent of the price-saving comfort floor above: nobody is home during
a vacation, so the setback may sag further than it would with someone
actually living in the house.

### Managing plans

Two ways, both writing to the same options:

- **Settings → Devices & services → TrueTemp → Configure → Vacation
  plans** — a native options-flow add/edit/remove/reorder menu, no card
  required.
- **The `truetemp-vacation-card`** — a dashboard card, sibling to the main
  card, that lists plans (with an "active" badge on whichever one is
  currently winning), and adds/edits/removes/reorders them. Add it with:

  ```yaml
  type: custom:truetemp-vacation-card
  entity: sensor.<name>_vacation_status
  ```

  Any entity from the zone works — the arm switch and the config entry to
  address are discovered automatically, same as the main card.

Three services back both the card and the options flow, and can also be
called directly from automations/scripts — all addressed by
`config_entry_id` (via a `config_entry` selector scoped to the `truetemp`
integration) rather than an entity, since no HA entity models "one vacation
plan":

| Service | Fields | Does |
| --- | --- | --- |
| `truetemp.vacation_plan_set` | `config_entry_id`, `id` (omit to create), `name`, `enabled`, `recurrence`, `min_temp_c`, plus whichever recurrence-specific fields apply | Upserts one plan |
| `truetemp.vacation_plan_remove` | `config_entry_id`, `id` | Deletes a plan by id |
| `truetemp.vacation_plan_reorder` | `config_entry_id`, `id`, `direction` (`up`/`down`) | Swaps a plan with its neighbour — the only way to change which plan wins an overlap |

`vacation_plan_set` runs the same overlap check as the options flow and
raises a translated `ServiceValidationError` (readable in the Developer
Tools UI or a failed automation) rather than silently saving an ambiguous
list.

`sensor.<name>_vacation_status`'s state is one of `inactive`/`invalid`/
`scheduled`/`setback`/`ramping`/`done` — whichever phase the currently-winning
plan (if any) is in — and its attributes carry the whole plan list plus
`active_plan_id`, so the card only needs one entity to read.

---

## Entities

Nine per zone, plus the vacation-plan entities above.

| Entity | What it is |
| --- | --- |
| `climate.<name>` | The thermostat: target, on/off, price saving preset |
| `sensor.<name>_compensated_outdoor_temperature` | The output when set to outdoor-sensor spoofing. Unavailable if you've picked one of the other two output modes instead |
| `sensor.<name>_heat_pump_offset` | The output when set to write the pump's own heat-curve-offset input. Unavailable outside that mode — exactly one of these three is ever live |
| `sensor.<name>_indoor_climate_target_temperature` | The target temperature pushed to a room-sensor `climate` entity when set to indoor-climate output mode. Unavailable outside that mode |
| `sensor.<name>_learned_offset` | What your house has taught it. **The one to graph** |
| `sensor.<name>_status` | `ok` / `degraded` / `error`, plus everything about learning and the full output breakdown (every term and a plain-language reason) in attributes |
| `switch.<name>_compensation_active` | Master on/off |
| `select.<name>_price_saving` | Comfort first / Balanced / Savings first |
| `select.<name>_cold_caution` | How careful to be in the cold |

Turning compensation **off** does not turn the heating off. It publishes the
raw outdoor temperature and your pump runs its own curve exactly as before — a
true no-op. It also pauses learning, necessarily: an integral controller cannot
learn from an output that is not applied, because it never sees the response to
its own action.

### Watching it settle

`sensor.<name>_status` attributes carry the whole picture: `learning_progress_pct`,
`outdoor_bands_covered_pct`, `response_time_min`, `wind_down_time_min`,
`pump_at_capacity`, `learning_paused` and why.

#### `sensor.<name>_status` attribute reference

Grouped in the order they're built in `sensor.py`. `disabled` (a literal string,
not the boolean `False`) marks a value that is `None` only because the feature
that would fill it is switched off — see the module docstring in `sensor.py`.

**Source health**

| Attribute | Meaning |
| --- | --- |
| `outdoor_sensor_ok` | The hard-required source. `False` here is what makes the state `error`. |
| `last_error` | The exception behind the *current* failure, or `None` once recovered — cleared as soon as `outdoor_sensor_ok` goes back to `True`. |
| `last_error_at` | Local ISO timestamp of when the current failure started, or `None`. |
| `indoor_sensor_ok` | Indoor reading available this cycle. |
| `indoor_sensor_count` | `{usable, configured}` — how many of the configured indoor sensors are currently reporting. |
| `indoor_sensor_readings` | Entity id → °C for every configured indoor sensor currently reporting. |
| `indoor_aggregation_mode` | `average` or `lowest` — how multiple indoor sensors are combined. See [Multiple indoor sensors](#multiple-indoor-sensors). |
| `wind_forecast_ok` | Present only if wind input is enabled. Weather-entity forecast had a wind reading. |
| `cloud_sun_forecast_ok` | Present only if sun input is enabled. Weather-entity forecast had a cloud reading. |
| `price_ok` | Present only if a price sensor is configured. Today's/tomorrow's forecast was readable. |

Each source above also raises a Home Assistant Repair (`homeassistant.helpers.issue_registry`)
while it is down — `outdoor_sensor_unavailable` at `ERROR` severity, the rest at `WARNING` — so a
failing sensor surfaces in the Notifications bell and Settings > Repairs without needing a
separate automation. One issue per source per config entry; cleared the moment that source reads
again, and on unload/reload so nothing outlives the entry. See `TrueTempCoordinator._sync_source_issue`.
Indoor is the exception with more than one issue key — see
[Multiple indoor sensors](#multiple-indoor-sensors) for the three-way split.

**Output breakdown** — every term behind the published value, why it's what it is now, regardless of which output sensor is actually live:

| Attribute | Meaning |
| --- | --- |
| `raw_outdoor_temp_c` | The unmodified reading from your outdoor sensor. |
| `indoor_temp_c` | Current indoor reading, or `None` if unavailable. |
| `indoor_target_c` | The target you set on `climate.<name>`. |
| `effective_indoor_target_c` | Target after price compensation's deliberate sag is applied — what the loop is actually steering toward this cycle. |
| `learned_offset_c` | The learner's contribution — same number as the `learned_offset` sensor's state. |
| `wind_adjustment_c` | Feedforward wind correction, zero if wind input is off. |
| `sun_adjustment_c` | Feedforward solar correction, zero if sun input is off. |
| `price_adjustment_c` | Price-braking correction, zero if price compensation is off or not engaged. |
| `outdoor_preramp_c` | Anticipatory push (≤ 0) from a forecast outdoor-temperature drop. `disabled` if the forecast lookahead is off. |
| `wind_preramp_c` | Anticipatory push (≤ 0) from forecast rising wind. `disabled` if the lookahead is off; zero if the wind input is off. |
| `weather_preramp_c` | The two above, summed and clamped to a shared 3 °C budget — the number that actually reaches the published value. `disabled` if the lookahead is off. |
| `weather_preramp_in_min` | Minutes until the forecast change currently driving the pre-ramp. `disabled` if the lookahead is off. |
| `weather_preramp_active` | `True` while the pre-ramp is deliberately holding the house above target — tells the learner to freeze, exactly as `price_braking` does in the other direction. |
| `sun_precool_c` | Anticipatory pull-back (≥ 0) from forecast gaining sun, timed off the measured fall time. `disabled` if the lookahead is off; zero if the sun input is off. |
| `sun_precool_in_min` | Minutes until the forecast sun increase currently driving the pre-cool. `disabled` if the lookahead is off. |
| `sun_precool_active` | `True` while the pre-cool is deliberately holding the house below target — tells the learner to freeze, same mechanism as `weather_preramp_active` in the opposite direction. |
| `wind_speed_ms` | Wind speed read from the weather entity's forecast. |
| `cloud_coverage_pct` | Cloud cover read from the weather entity's forecast. `disabled` if sun input is off and nothing was fetched. |
| `solar_effect` | Fraction (0–1) of full solar gain available right now — pure geometry and cloud cover, computed regardless of whether the sun term is enabled (see `solar_effect_of` in `heuristic.py`). |
| `current_price` | Current price from the configured price sensor. Read regardless of whether price compensation is switched on. |
| `price_shift_applied_c` | How far price compensation is currently holding indoor away from target. |
| `heating_hard_limit_engaged` | `True` at or above the fixed 20°C hard limit — the published value is forced to the warm ceiling (`OUTPUT_SANITY_MAX_C`) regardless of what the learned offset, wind, sun or price terms would otherwise produce. |
| `reason` | Plain-language explanation of the cycle's output, shown on the card. |
| `price_comfort_tier` | Current price-saving preset (`low`/`mid`/`high`), mirroring `select.<name>_price_saving`. |
| `cold_caution` | Current cold-caution preset, mirroring `select.<name>_cold_caution`. |
| `price_response` | Price band response, 0–1, before the cold-caution and recovery-feasibility tapers are applied. |
| `cold_brake_factor` | How much price-braking authority survives the cold (caution ramp × measured recovery feasibility), 0–1. |
| `allowed_sag_c` | How far indoor is currently allowed to coast below target for price. |
| `upcoming_spike_in_min` | Minutes until a forecast price spike close enough to pre-brake for. `disabled` if price compensation is off. |
| `precharge_active` | `True` while heat is being deliberately banked ahead of an expensive hour (Savings-first tier only). |
| `price_braking` | `True` while price compensation is deliberately holding the house below target — tells the learner to freeze rather than read the sag as error. |
| `lead_minutes_effective` | How far ahead of a spike braking starts — one measured wind-down time. |
| `price_band_start` | Price at which braking begins today. `disabled` if price compensation is off. |
| `price_band_full` | Price at which braking is at full authority today. `disabled` if price compensation is off. |
| `price_median` | Today's median price — the "ordinary for today" line. Read regardless of whether price compensation is switched on. |
| `today_price_spread_c` | Today's peak-minus-median price spread, in the price sensor's own units. `None` with no usable day-ahead forecast. Read regardless of whether price compensation is switched on. |
| `seasonal_reference_spread_c` | The median of the last ~30 stored days' spread — the seasonal reference `price_significance_factor`'s relative term compares today against. `None` during cold start (fewer than 5 stored days). |
| `price_significance_factor` | Combined 0–1 taper on braking/pre-charge authority for how economically meaningful today's price swing is — see `heuristic.price_significance()`. 1.0 means no damping. |
| `recommended_compensated_outdoor_temp_c` | What `compensated_outdoor_temperature` would show, computed even in heat-curve-offset mode or while compensation is off. |
| `total_adjustment_c` | `recommended_compensated_outdoor_temp_c` minus `raw_outdoor_temp_c` — taken from the published value, not summed from the terms above, so it reflects the output sanity clamp when that bites. |
| `recommended_heat_pump_offset` | What `heat_pump_offset` would show, computed even in outdoor-spoof mode or while compensation is off. |
| `active` | Whether compensation is currently switched on (`switch.<name>_compensation_active`). |
| `output_mode` | Which output sensor is the one actually wired to the pump: outdoor-spoof, heat-curve-offset, or indoor-climate. |

**How learning is going**

| Attribute | Meaning |
| --- | --- |
| `learning_progress_pct` | Overall authority ramp — how much the learner currently trusts its own corrections. |
| `outdoor_bands_covered_pct` | Fraction of outdoor-temperature bands with at least one closed-loop sample. |
| `authority_limit_c` | Current per-cycle offset ceiling, widening from ±1 °C to ±5 °C over the first ~3 days. |
| `capacity_ceiling_c` | This band's learned capacity ceiling — beyond it, spoofing further isn't shown to help. |
| `settling_time_h` | The integrator's time constant, driven by the measured response time. |
| `samples_learned` | Total closed-loop samples across all bands. |
| `samples_this_band` | Closed-loop samples in the band currently occupied. |
| `pump_at_capacity` | `True` if the current band is at its learned capacity ceiling. |
| `learning_paused` | `True` if the integrator is frozen this cycle (e.g. during price braking). |
| `learning_paused_because` | Present only while `learning_paused` is `True`. Plain-language reason. |
| `recovery_rate_c_per_h` | Present only once a non-zero recovery rate has been measured for this band. |

**Open-loop baseline** — what the pump's own curve does with compensation off, recorded per band so an install that's never been switched on still has something to show:

| Attribute | Meaning |
| --- | --- |
| `baseline_coverage_pct` | Fraction of bands with at least one open-loop observation. |
| `baseline_indoor_c` | Present only once this band has an observation. Where the raw curve settles the house, in this band. |
| `baseline_deficit_c` | Present alongside `baseline_indoor_c`. How far short of target that settled temperature is. |
| `baseline_samples_this_band` | Open-loop observations recorded for the current band. |
| `baseline_reason` | Present only while compensation is off. Why the baseline did or didn't update this cycle. |
| `baseline_seeded_bins` | Present only on the cycle compensation switches on. How many bands were seeded from their baseline. |
| `offset_seeded_this_band` | `True` if the current band's offset came from the baseline rather than closed-loop samples. |

**How the house responds**

| Attribute | Meaning |
| --- | --- |
| `response_time_min` | Measured (or default, see `response_time_measured`) time from commanded heat to a measurable indoor change. |
| `wind_down_time_min` | Measured (or default) time from the pump backing off to the house actually stopping gaining. Typically the longer of the two — see [It measures how long your house takes](#it-measures-how-long-your-house-takes). |
| `response_time_measured` | `False` while still using the default for your emitter type. |
| `wind_down_time_measured` | `False` while still using the default for your emitter type. |

**Plumbing**

| Attribute | Meaning |
| --- | --- |
| `data_logging_enabled` | Whether local JSONL logging is switched on. |
| `data_log_path` | Present only while logging is enabled. Full path to this zone's log file. |

Four attributes were removed as duplicates: `indoor_data_available`,
`wind_data_available`, `cloud_data_available` and `price_data_available` used
to leak in alongside `indoor_sensor_ok`, `wind_forecast_ok`,
`cloud_sun_forecast_ok` and `price_ok` — identical booleans under a second,
less friendly name, an artifact of building the breakdown from
`dataclasses.asdict()`. A fifth, `model_version`, was an internal versioning
marker with no consumer as an attribute (unlike the learner's own model
version, which is load-bearing for state-store staleness checks). All five
are gone as of this write-up.

**Download diagnostics** (on the integration's entry page) additionally dumps
the *full offset table* — every band's learned offset, capacity ceiling,
recovery rate and sample count. The `learned_offset` sensor only shows the band
you are in right now, so the table is what distinguishes "still filling in"
from "learned something odd at −10 °C and has not been back since".

Set the `custom_components.truetemp` logger to `debug` for a per-cycle
line from the learner and the lag estimator.

### Dashboard card

A card ships with the integration and registers itself automatically:

```yaml
type: custom:truetemp-card
entity: sensor.<name>_status
```

Any entity from the zone works — the rest are discovered automatically.

---

## What is deliberately not configurable

Everything that used to be. `k_indoor`, `k_wind`, `k_sun`, `k_price`, three
cold-taper thresholds, two price thresholds, a pre-charge boost, an update
interval, an upper comfort bound and a manual/auto tuning mode have all been
removed. Each is now either measured from your house or fixed at a value with a
physical justification.

The dividing line: **config describes the occupant, never the building.** What
temperature you want and how much comfort you will trade for money are yours.
How fast your house loses heat, how much a nudge moves it, and how long that
takes are measurements.

Two constants are worth naming because they could look like hidden tuning:

- **Sun and wind gains are fixed**, not learned. Both correlate strongly with
  outdoor temperature, and a fit of the solar term against real data once came
  out *negative* — sunlight cooling the house. That fit came from a house whose
  indoor sensor was in the basement, where the solar gain genuinely is absent
  while the sun-and-cold correlation is not, so the regression had every reason
  to land where it did. A fixed sensible constant has a much better failure
  mode: any bias in the daily average is absorbed by the learner within a day,
  so being slightly wrong costs a little intra-day ripple and no steady error at
  all. A learned gain with the wrong sign would actively fight the loop.
- **One degree of spoof is treated as one degree of indoor change.** Spoofing
  +1 °C and letting indoor fall 1 °C change the indoor–outdoor gap identically.
  Getting this wrong changes only how *fast* the loop converges, never where it
  converges to — which is the property of integral action the whole design
  rests on.

---

## History: what this replaced, and why

Earlier versions carried a grey-box RC thermal model fitted online by recursive
least squares, a model-predictive planner built on top of it, and a derivation
that inverted the model into control gains. All three were shadow-mode only.

On 2026-08-07 the RC model was validated against 1,193 records of real data.
It failed, and not marginally:

- Batch **R² = 0.029**, and **−0.7% skill against persistence** — no better
  than predicting "nothing changes".
- The envelope time constant was not statistically significant and sat pinned
  at its 500 h clip bound on 31% of samples. The sensor reporting it had been
  showing a guardrail, not an estimate.
- The solar coefficient came out **negative** and significant — sunlight
  cooling the house. The indoor sensor in that house was in the basement, which
  the sun never reaches, so there was no solar gain to find; what the fit picked
  up instead was that clear sunny weather is also cold weather.

The cause was structural rather than a bug: identifying several continuous
physical parameters from a 0.1 °C-quantised indoor signal in a closed loop is a
genuinely hard system-identification problem, and 63% of the transitions in the
data read exactly zero change.

Integral action asks a much smaller question and answers it from the same data.
Integrating a quantised error over hours is precisely what integrators are good
at, where differentiating it one sample at a time is what destroyed the fit. It
needs no excitation, no trust gate, no confidence threshold, and its worst case
is a clamped offset rather than a division by a badly estimated gain.

The three modules were deleted — about 3,400 lines of production code and 1,650
of tests. They remain in git history.

The opt-in logging that made that validation possible is unchanged and still
here. It is how the current controller should be held to the same standard.

---

## Local history logging

Off by default. When on, appends one JSON line per cycle to
`/config/truetemp_data/<entry_id>.jsonl`: raw physical inputs, the
learner's internal state, and the price forecast the decision actually used.
Purely local.

**Filter on `learner_stepped` when replaying.** A record is written on every
cycle, including the extra refreshes a recovering source triggers, but the
learner only advances on its fixed 15-minute clock. Consecutive records inside
one interval therefore repeat identical `learner_*` values while the raw inputs
differ, and treating every line as a decision is exactly the irregular-cadence
mistake that made the previous model's data unusable.

This exists because Home Assistant's recorder purges history (commonly ~10 days)
and its long-term statistics keep only hourly aggregates — too coarse to judge a
controller or replay a candidate change offline.

Files rotate to a gzipped sibling at 10 MB and are never deleted automatically.

---

## Upgrading from 0.3.x

**0.4.0 replaced the single holiday scenario with the multi-plan vacation
system above.** This is a breaking change, not a migration — an existing
armed holiday scenario is not carried over into a plan:

- `date.*_holiday_start`, `date.*_holiday_end` and
  `number.*_holiday_target_temperature` are gone entirely — no per-plan
  entity exists any more (see [Vacation plans](#vacation-plans) for why).
  Automations referencing them will break.
- `switch.*_holiday_mode` and `sensor.*_holiday_status` were renamed to
  `switch.*_vacation_mode` and `sensor.*_vacation_status`.
- If you had a holiday scenario armed, recreate it as a plan (via the
  options flow or the new card) after upgrading.

---

## Upgrading from 0.1.x

The RC model, MPC planner and auto-tune are gone, along with their sixteen
entities and most of the options. Existing entries keep working — unknown
stored options are simply ignored — but:

- Automations referencing `sensor.*_rc_model_*`, `sensor.*_mpc_*`,
  `sensor.*_auto_tune_*` or `select.*_tuning_mode` will break; those entities
  no longer exist.
- `sensor.*_indoor_temperature`, `*_outdoor_temperature`,
  `*_indoor_temperature_error`, `*_price_shift_applied` and `*_power_draw` are
  now attributes on the main sensor or on `status`.
- Learning starts fresh. The old estimator's state is not carried over — it
  failed validation, so seeding from it would import its error.
- **Compensation now defaults to on.** An existing install keeps whatever the
  switch was last set to.
