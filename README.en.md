# TrueTemp

🇸🇪 [Läs det här på svenska](README.md)

**Your heat pump is guessing what the temperature is outside. TrueTemp
teaches it to guess right — so your home actually reaches the temperature you
asked for.**

Support on [Discord](https://discord.gg/6VmjrXA4h)

---

## The problem

Most heat pumps run on a "weather curve": they look at the outdoor
temperature and calculate how hard to work from a fixed formula. That formula
never checks whether your house is actually warm enough — so if it's a little
off, it stays a little off, all winter, and there's nothing to adjust.

TrueTemp is a free add-on for Home Assistant that fixes this. It
watches your indoor temperature and quietly corrects the outdoor-temperature
number your heat pump sees, so the pump's own logic ends up landing on the
temperature you actually want. It figures this out on its own, from your
house, over a few days — there are no settings to fiddle with.

---

## Features

- 🧠 **Learns on its own** — no numbers to tune, no expert knowledge needed.
- 🎯 **Hits the temperature you asked for**, even when the pump's built-in
  curve doesn't.
- 🏠 **Reads from more than one room** (optional) — combine up to five
  sensors, either averaged to smooth out a single sensor's noise, or set to
  heat to the coldest room so it doesn't hide behind a warm average.
- 🔒 **Safe by default** — installs in "watch only" mode and shows you what it
  *would* do before it touches anything. Turn it off any time and the pump
  runs exactly as it did before.
- 💶 **Can save you money** (optional) — shifts heating to cheaper hours if you
  connect an electricity price sensor (e.g. Nord Pool), without you noticing
  the difference.
- ☀️💨 **Accounts for sun and wind** (optional) — for homes where sunlight
  genuinely warms the room, or where draughts are a real issue.
- 🧳 **[Holiday mode](#holiday-mode)** (optional) — set up one or more
  named trips, one-off or recurring, and let TrueTemp work out exactly
  when to start warming the house back up so it's warm right as you get
  home.
- 📊 **[A dashboard card is included](docs/card.png)**, showing what it's
  doing and why.
- 🔌 **Works with most heat pumps** — all it needs is a pump that reads an
  outdoor-temperature sensor, which almost all of them do.

---

## Got an older heat pump that can't be smart-controlled?

Some heat pumps have no app, no cloud service, no way to talk to Home
Assistant at all — just a wired outdoor sensor. TrueTemp still needs
some way to hand its corrected number to the pump.

**[Ohm on WiFi Plus](https://www.ohmigo.io/product-page/ohm-on-wifi-plus)
from Ohmigo** solves exactly this. It's a small WiFi device that takes over
your heat pump's existing outdoor-temperature sensor and lets you (or
TrueTemp) decide what temperature the pump should believe it is
outside. If you have an older pump with no smart features of its own, it's
the easiest way to make it controllable — and TrueTemp can talk to it
directly, with nothing else to configure.

NOTE! Some heat pumps CAN be sensitive to settings writes "wearing out" the memory. We write as few updates as possible, but it's worth keeping in mind. An Ohm on WiFi doesn't have that problem.

---

## Got a newer heat pump with its own curve offset?

Many newer heat pumps (NIBE, for example) instead have a built-in setting for
shifting the heat curve up or down — often called a "curve offset" or "heat
curve offset". If your pump has one, TrueTemp can write its calculated value
there directly, instead of pretending to be the outdoor sensor. You pick
either way under **Outgoing sensors** in the integration's settings — not
both, since they'd compensate for the same thing twice.

---

## Got a heat pump or thermostatic valves with their own room sensor?

Some heat pumps and thermostatic radiator valves (TRVs) are instead
controlled through their own `climate` entity in Home Assistant with its own
target temperature, tied to a room sensor — not an outdoor curve. If you have
one of those, TrueTemp can write its calculated target there directly,
instead of pretending to be the outdoor sensor or adjusting a curve offset.
Just like the two other methods, you pick this under **Outgoing sensors** in
the integration's settings — only one of the three at a time, for the same
reason as above.

---

## Installation

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smefa&repository=TrueTemp-for-Heat-Pumps&category=integration" target="_blank">
  <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.">
</a>

1. Open **HACS** in Home Assistant.
2. Click the three dots in the top-right corner → **Custom repositories**.
3. Add `https://github.com/smefa/TrueTemp-for-Heat-Pumps` with category **Integration**.
4. Search for **TrueTemp** in HACS and install it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for
   **TrueTemp**.

You'll be asked four simple questions:

- **Which sensor measures the room you actually live in?** Avoid a
  basement, a spot in direct sunlight, or somewhere draughty — this is the
  reading everything else is learned from. You can add up to four more
  sensors right here (or later in settings) and choose whether TrueTemp
  averages them or heats to the coldest room.
- **Which outdoor sensor should it use?** Your own sensor if you have one,
  otherwise a weather service works fine.
- **What temperature do you want indoors?**
- **Radiators, underfloor heating, or both?** This is only used for the
  first few days, until it has measured how your specific house responds.

Finally, connect the new `..._compensated_outdoor_temperature` sensor to your
heat pump's outdoor-temperature input — or let TrueTemp send it there
automatically (see below).

**Nothing is touched until you say so.** After installation, TrueTemp
only watches and calculates — it shows you what it *would* send to your pump
without actually sending it. Turn it on with the TrueTemp switch once
you're happy, and turn it off again at any time with no side effects.

### More settings — all optional

Everything below is optional and can be skipped:

- **Extra indoor sensors** — combine up to four more with the one above,
  either averaged or set to heat to whichever room is coldest.
- **Sun and wind compensation** — for homes where these genuinely affect the
  room you're measuring in.
- **Acting on the weather forecast** — starts heating a little harder before a
  cold front or a rising wind actually arrives, so the house doesn't have to
  cool down first. That part only ever adds heat, never takes it away. Sun
  works the other way around: it holds heat back before a burst of sunshine
  arrives, so the house doesn't overheat once the sun's own warmth lands.
- **Electricity price savings** — shift heating away from the most expensive
  hours of the day.
- **Where to send the result** — a Home Assistant entity, and/or a direct
  connection to an Ohm on WiFi / Ohmigo device (see above); your pump's own
  curve offset, if it has one (see above); or a `climate` entity with its own
  room sensor, for pumps and TRVs that have one (see above).
- **Local logging** — a detailed log file on your own machine, for anyone who
  wants to dig into the numbers. Nothing is sent anywhere.

---

## Holiday mode

Going away? Holiday mode turns the heat down while you're gone and works
out exactly when the house needs to start warming back up, so it's warm
right as you get home — not an hour too early or too late.

You set up one or more named **plans** instead of just a single trip. Each
plan is switched on/off individually and repeats in one of three ways:

- **Once** — a start and end date, for a specific trip.
- **Weekly** — e.g. "Friday 18:00 to Monday 07:00", for the cabin every
  weekend.
- **Yearly** — e.g. "Jul 1–14", for the annual summer holiday (works even
  across New Year's).

Plans are managed from their own dashboard card, separate from the main
card, where you add, edit, delete and reorder them (this can also be done
from the integration's own settings, without the card). On top of the
plan list, a single master switch (**Holiday mode**) turns every plan off
at once regardless of which ones are individually enabled — handy for "I'm
home early".

How it works:

1. At the moment a plan starts, the target drops to that plan's own
   setback temperature in one sharp step.
2. TrueTemp works out when the ramp back up needs to start based on how
   fast your specific house has historically warmed up — the same
   measurement the rest of the integration is built on — instead of
   guessing a fixed recovery time. That way the house has time to warm up
   without forcing the heat pump's backup heat to kick in.
3. The house is back at your normal target by 15:00 on the plan's end
   date. If there isn't enough time for a gentle ramp, it starts ramping
   immediately at the start instead, and the status shows that happened.
4. The setback never drops below a fixed frost-safety floor.

If two enabled plans' time windows would overlap, list order decides which
one wins — TrueTemp warns and refuses to save the change rather than
letting that happen silently. The card also shows which plan, if any, is
currently active.

---

## Want the technical details?

The plain-language version above is the whole story for most people. If
you're curious how the learning actually works under the hood, or you're a
developer, see the [technical reference](docs/TECHNICAL.md). Built with Claude code.

---

## License

MIT — see [LICENSE](LICENSE).
