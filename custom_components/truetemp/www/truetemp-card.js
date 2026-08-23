// TrueTemp dashboard card.
//
// Registered automatically as a frontend resource at startup, so there is
// nothing to add under Settings -> Dashboards -> Resources.
//
// This used to be a 23-entity feature-test grid whose job was proving every
// sensor a zone exposed was reachable. A zone now has seven entities, so the
// card can be the thing you actually want on a wall tablet: what it is doing
// right now, and how well it knows your house.
//
// Add it with:
//     type: custom:truetemp-card
//     entity: sensor.<name>_status
//
// Any entity belonging to the zone works — the rest are discovered through the
// frontend entity registry, so renaming entities never breaks it.
//
// Optional config:
//     bands: false       hide the per-band offset table
//     price: false       hide the price section
//     priceGraph: false  hide the price-forecast graph
//     sources: false     hide the source-health row

import { STRINGS as EN } from "./lang/en.js";
import { STRINGS as DE } from "./lang/de.js";
import { STRINGS as SV } from "./lang/sv.js";

// Registry translation_key per entity — stable across languages, unlike the
// entity_id, whose object_id is slugified from the *translated* friendly name
// at creation time. A Swedish install ends up with
// sensor.<name>_varmepumpsoffset rather than sensor.<name>_heat_pump_offset,
// so matching on entity_id suffix (as this card used to) silently fails on
// any non-English install. translation_key is set directly by the Python
// entity (see _attr_translation_key in sensor.py/select.py/switch.py) and
// never changes with locale or a user's custom friendly name.
const TRANSLATION_KEYS = {
  status: "status",
  compensated: "compensated_outdoor_temperature",
  heatPumpOffset: "heat_pump_offset",
  offset: "learned_offset",
  active: "active",
  tier: "price_comfort_tier",
  caution: "cold_caution",
  vacation: "vacation_status",
};

// Fallback for the rare case a registry entry lacks translation_key (e.g. a
// very old frontend). English-only, so still subject to the bug above, but
// better than nothing.
const SUFFIXES = {
  status: "status",
  compensated: "compensated_outdoor_temperature",
  heatPumpOffset: "heat_pump_offset",
  offset: "learned_offset",
  active: "compensation_active",
  tier: "price_saving",
  caution: "cold_caution",
  vacation: "vacation_status",
};

// Same six-state phase set as holiday.py's HolidayResult / the vacation-plans
// card. "inactive" and "done" mean nothing is currently sagging the target,
// so those two produce no note below.
const VACATION_NOTE_KEYS = {
  invalid: "noteVacationInvalid",
  scheduled: "noteVacationScheduled",
  setback: "noteVacationActive",
  ramping: "noteVacationRamping",
};

// The sentinel the sensors publish for an attribute whose feature is switched
// off (see sensor.ATTR_DISABLED). Rendered as its own thing rather than as a
// missing value, because "off" and "should be here and isn't" are exactly what
// this card exists to tell apart.
const DISABLED = "disabled";

// UI text, keyed by the two-letter language HA reports on hass.language.
// Lives one file per language under lang/ rather than inline here, matching
// the wording already used for these same concepts in
// translations/{en,de,sv}.json (Kältevorsicht/Försiktighet vid stark kyla for
// cold caution, Prisbesparing/Preisersparnis for saving level, etc.) so a
// term reads the same on the card as it does in the entity/options UI.
//
// de.js/sv.js only need to carry the keys they translate — anything missing
// falls back to English here rather than leaving a hole in the card.
const STRINGS = {
  en: EN,
  de: { ...EN, ...DE },
  sv: { ...EN, ...SV },
};

// hass.language is a two-letter (or region-tagged, e.g. "en-GB") code; fall
// back to English for anything we have not translated rather than showing a
// half-translated card.
function pickLang(hass) {
  const raw = (hass && (hass.language || (hass.locale && hass.locale.language))) || "en";
  const base = String(raw).split("-")[0].toLowerCase();
  return STRINGS[base] ? base : "en";
}

function fmt(template, vars) {
  return template.replace(/\{(\w+)\}/g, (_, key) => (key in vars ? vars[key] : `{${key}}`));
}

class TrueTempCard extends HTMLElement {
  constructor() {
    super();
    // While the pointer is over the card, hold off rebuilding innerHTML (see
    // the render-pause note in _render()) so a hover-tooltip timer started on
    // a row/badge isn't reset out from under the user before it can fire.
    this._hovering = false;
    this.addEventListener("mouseenter", () => {
      this._hovering = true;
    });
    this.addEventListener("mouseleave", () => {
      this._hovering = false;
      if (this._renderPending) {
        this._renderPending = false;
        this._render();
      }
    });

    // Delegated rather than bound per-cell, since innerHTML gets torn down
    // and rebuilt on every render (see _render()) — a listener attached to a
    // cell itself would be gone by the next tick.
    this.addEventListener("click", (ev) => {
      const el = ev.target.closest("[data-entity]");
      if (el) this._fireMoreInfo(el.dataset.entity);
    });
    this.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const el = ev.target.closest("[data-entity]");
      if (!el) return;
      ev.preventDefault();
      this._fireMoreInfo(el.dataset.entity);
    });
  }

  // Opens HA's own more-info dialog (History tab included) for a cell backed
  // by a real entity. Cells that are only attributes on `status`/`offset`
  // have no entity to open — see the module docstring on why most of the
  // card's numbers were deliberately never promoted to their own entity.
  _fireMoreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId },
      }),
    );
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error(STRINGS[pickLang(this._hass)].setEntityError);
    }
    this._config = config;
    this._entities = null;
  }

  getCardSize() {
    const base = this._config && this._config.bands === false ? 7 : 11;
    return this._config && this._config.priceGraph === false ? base : base + 2;
  }

  static getConfigElement() {
    return document.createElement("truetemp-card-editor");
  }

  // Picks any _status entity that belongs to this integration, so dropping
  // the card from the picker works before the user has touched YAML.
  static getStubConfig(hass) {
    const entityId = Object.keys(hass.entities || {}).find(
      (id) => hass.entities[id].platform === "truetemp" && id.endsWith("_status"),
    );
    return { entity: entityId || "" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entities) {
      this._entities = this._discover(hass);
    }
    this._render();
  }

  // Find the zone's other entities via the device the configured one belongs
  // to. Falls back to the configured entity alone if the registry is not
  // reachable, so the card degrades to something rather than nothing.
  _discover(hass) {
    const found = { climate: null };
    Object.keys(SUFFIXES).forEach((key) => {
      found[key] = null;
    });

    const configured = this._config.entity;
    const entry = hass.entities && hass.entities[configured];
    const deviceId = entry && entry.device_id;
    if (!deviceId || !hass.entities) {
      found.status = configured;
      return found;
    }

    Object.keys(hass.entities).forEach((entityId) => {
      const info = hass.entities[entityId];
      if (info.device_id !== deviceId) return;
      if (entityId.startsWith("climate.")) {
        found.climate = entityId;
        return;
      }
      if (info.translation_key) {
        Object.entries(TRANSLATION_KEYS).forEach(([key, tKey]) => {
          if (info.translation_key === tKey) found[key] = entityId;
        });
        return;
      }
      Object.entries(SUFFIXES).forEach(([key, suffix]) => {
        if (entityId.endsWith(`_${suffix}`)) found[key] = entityId;
      });
    });
    if (!found.status) found.status = configured;
    return found;
  }

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    return this._hass.states[entityId] || null;
  }

  // Everything interpolated below reaches the DOM as markup, and several of
  // these strings are user-controlled (entity friendly names, the free-text
  // reason). Cheap to escape, and the alternative is a stored-XSS hole on a
  // dashboard.
  _esc(value) {
    return String(value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  _tooltip(t, key, vars) {
    const text = t[key] || EN[key];
    if (!text) return "";
    return ` title="${this._esc(vars ? fmt(text, vars) : text)}"`;
  }

  // Matches truetemp-vacation-card.js's own formatter, since a date/time
  // shown here should read identically to the same value on that card.
  _fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  _num(value, digits = 1, unit = "") {
    if (value === DISABLED) return DISABLED;
    const parsed = Number(value);
    if (value === null || value === undefined || value === "" || Number.isNaN(parsed)) {
      return "—";
    }
    return `${parsed.toFixed(digits)}${unit}`;
  }

  // A pair may carry a 4th element: the entity_id whose current state this
  // cell's value came from verbatim. Only pass one when that's actually
  // true — see the call sites — since clicking must open history for the
  // number the user is looking at, not a same-named entity showing something
  // else right now.
  _cells(pairs, t) {
    return pairs
      .map(([k, v, explanation, entityId, titleVars]) => {
        const off = v === DISABLED ? " co-off" : "";
        const clickable = entityId && this._state(entityId) ? " co-clickable" : "";
        const display = v === DISABLED ? t.badgeDisabled : v;
        const title = explanation ? this._tooltip(t, explanation, titleVars) : "";
        const interactive = clickable
          ? ` data-entity="${this._esc(entityId)}" role="button" tabindex="0"`
          : "";
        return `<div class="co-cell${off}${clickable}"${title}${interactive}><span>${this._esc(k)}</span><b>${this._esc(display)}</b></div>`;
      })
      .join("");
  }

  // The offset table, one row per outdoor band. The sensor's own state is only
  // the band currently occupied, which cannot distinguish "needs +1.4°C
  // everywhere" from "needs +1.4°C here and something odd at -10°C it has not
  // revisited since". Every band is shown, including empty ones, so the gaps in
  // coverage are as visible as the entries.
  //
  // Samples also folds in the open-loop baseline count per band — what the
  // raw curve settles the house at while compensation is off — so this table
  // is worth looking at on an install that has never been switched on: the
  // Samples column reads as pre-measurements (marked with *) instead of
  // empty. baseline_indoor_c rides along in the marker's tooltip rather than
  // its own column.
  _bandTable(offsetAttrs, t) {
    const bands = offsetAttrs.bands;
    if (!bands || typeof bands !== "object") return "";
    const current = offsetAttrs.outdoor_band;
    let extrapolated = false;
    let hasSeeded = false;
    let hasPreMeasured = false;

    const rows = Object.entries(bands)
      .map(([label, b]) => {
        const samples = Number(b.samples) || 0;
        const offsetC = Number(b.steady_offset_c) || 0;
        const seeded = b.seeded === true;
        if (seeded) hasSeeded = true;
        // A band can hold an offset on zero samples for two different reasons:
        // seeded from the open-loop baseline on activation, or nudged by a
        // neighbouring band's update without being credited a sample itself.
        // Worth telling apart — one is open-loop evidence for this exact band,
        // the other is a guess borrowed from next door.
        const spill = !seeded && samples === 0 && offsetC !== 0;
        if (spill) extrapolated = true;
        const marker = seeded
          ? `<i class="co-spill"${this._tooltip(t, "markerSeededTitle")}>s</i>`
          : spill
            ? `<i class="co-spill"${this._tooltip(t, "markerSpillTitle")}>~</i>`
            : "";
        const cls = [
          label === current ? "co-now" : "",
          samples === 0 ? "co-empty" : "",
          samples >= 50 ? "co-ok" : "",
        ]
          .filter(Boolean)
          .join(" ");
        const baselineIndoor = this._num(b.baseline_indoor_c, 1, " °C");
        const baselineSamples = Number(b.baseline_samples) || 0;
        const preMeasured = samples === 0 && baselineSamples > 0;
        if (preMeasured) hasPreMeasured = true;
        const preMeasuredTitle =
          t.markerPremeasuredTitle +
          (baselineIndoor !== "—" ? fmt(t.markerPremeasuredIndoor, { value: baselineIndoor }) : "");
        const samplesCell = samples >= 100
          ? `<span class="co-samples-done" title="${samples}">&check;</span>`
          : samples
            ? samples
            : preMeasured
              ? `${baselineSamples}<i class="co-spill" title="${this._esc(preMeasuredTitle)}">*</i>`
              : "—";
        return `<tr class="${cls}">
            <td>${this._esc(label)}${marker}</td>
            <td>${this._num(b.steady_offset_c, 2)}</td>
            <td>${this._num(b.capacity_ceiling_c, 1)}</td>
            <td>${this._num(b.recovery_rate_c_per_h, 3)}</td>
            <td>${samplesCell}</td>
          </tr>`;
      })
      .join("");

    return `
      <div class="co-section">${this._esc(t.sectionBands)}</div>
      <div class="co-scroll">
        <table class="co-table">
          <thead>
            <tr>
              <th>${this._esc(t.thOutdoorBand)}</th><th>${this._esc(t.thOffset)}</th><th>${this._esc(t.thCeiling)}</th>
              <th>${this._esc(t.thRecovery)}</th><th>${this._esc(t.thSamples)}</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="co-legend">
        ${this._esc(t.legendBase)}${extrapolated ? t.legendExtrapolated : ""}${hasSeeded ? t.legendSeeded : ""}${hasPreMeasured ? t.legendPremeasured : ""}.
      </div>`;
  }

  // Today/tomorrow's price curve, one bar per slot, coloured by the SAME
  // brake thresholds `price_band_start`/`price_band_full` use for the cells
  // above — but read from `price_forecast_band_start`/`_full`, which the
  // backend fills in unconditionally (see HeuristicResult's docstring on
  // those fields), so the graph classifies hours the same way whether price
  // compensation is switched on or off. Only falls back to a quartile split
  // of the forecast's own spread when those aren't available yet (too few
  // forecast points to judge a day's distribution, or no price data at all).
  _priceGraph(attrs, t) {
    const forecast = attrs.price_forecast;
    if (!Array.isArray(forecast) || forecast.length < 2) return "";

    const points = forecast
      .map((p) => ({ time: new Date(p.start).getTime(), price: Number(p.price) }))
      .filter((p) => Number.isFinite(p.time) && Number.isFinite(p.price))
      .sort((a, b) => a.time - b.time);
    if (points.length < 2) return "";

    const values = points.map((p) => p.price).sort((a, b) => a - b);
    const min = values[0];
    const max = values[values.length - 1];
    const range = Math.max(max - min, 0.0001);
    // Floor the scale a bit below the cheapest hour so its bar still draws a
    // visible sliver instead of vanishing at zero height.
    const scaleMin = min - range * 0.08;
    const scaleRange = Math.max(max - scaleMin, 0.0001);

    const bandStart = Number(attrs.price_forecast_band_start);
    const bandFull = Number(attrs.price_forecast_band_full);
    let lowMax, highMin;
    if (Number.isFinite(bandStart) && Number.isFinite(bandFull) && bandFull > bandStart) {
      lowMax = bandStart;
      highMin = bandFull;
    } else {
      lowMax = values[Math.floor(values.length * 0.25)];
      highMin = values[Math.floor(values.length * 0.75)];
    }
    // Absolute (not day-relative) colour scale so a given price always reads
    // the same regardless of that day's own spread: blue near 0 through
    // green/yellow/red up to purple around 3 (currency/kWh).
    const priceColor = (v) => this._priceColor(v);

    const width = 600;
    const height = 90;
    const t0 = points[0].time;
    const tEnd = points[points.length - 1].time;
    // Slot width comes from the gap to the next point; the last slot borrows
    // the previous gap since there is no point after it to measure against.
    const lastStep = points[points.length - 1].time - points[points.length - 2].time;
    const span = Math.max(tEnd - t0 + lastStep, 1);
    const x = (time) => ((time - t0) / span) * width;
    const y = (v) => height - ((v - scaleMin) / scaleRange) * height;

    const bars = points
      .map((p, i) => {
        const nextTime = i < points.length - 1 ? points[i + 1].time : p.time + lastStep;
        const barX = x(p.time);
        const barW = Math.max(x(nextTime) - barX, 0);
        const barY = y(p.price);
        return `<rect class="co-pg-bar" style="fill:${priceColor(p.price)}" x="${barX.toFixed(1)}" y="${barY.toFixed(1)}" width="${Math.max(barW - 1, 0).toFixed(1)}" height="${Math.max(height - barY, 0).toFixed(1)}"><title>${this._esc(this._fmtDateTime(new Date(p.time).toISOString()))}: ${this._esc(this._num(p.price, 2))}</title></rect>`;
      })
      .join("");

    const now = Date.now();
    const nowLine =
      now >= t0 && now <= tEnd
        ? `<line class="co-pg-now" x1="${x(now).toFixed(1)}" x2="${x(now).toFixed(1)}" y1="0" y2="${height}"></line>`
        : "";

    return `
      <div class="co-section">${this._esc(t.sectionPriceGraph)}</div>
      <div class="co-pricegraph"${this._tooltip(t, "explainPriceGraph")}>
        <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${bars}${nowLine}</svg>
      </div>
      <div class="co-pg-legend">
        <span class="co-pg-swatch" style="background:${priceColor(lowMax / 2)}"></span>${this._esc(t.legendPriceLow)} (&lt; ${this._esc(this._num(lowMax, 2))})
        <span class="co-pg-swatch" style="background:${priceColor((lowMax + highMin) / 2)}"></span>${this._esc(t.legendPriceMid)} (${this._esc(this._num(lowMax, 2))}–${this._esc(this._num(highMin, 2))})
        <span class="co-pg-swatch" style="background:${priceColor(Math.max(highMin, (highMin + 3) / 2))}"></span>${this._esc(t.legendPriceHigh)} (&ge; ${this._esc(this._num(highMin, 2))})
      </div>`;
  }

  // Continuous colour scale for the price graph: blue at 0 -> green -> yellow
  // -> red -> purple at ~3 (currency/kWh), interpolated linearly per segment.
  _priceColor(v) {
    const stops = [
      [0, [33, 150, 243]], // blue
      [0.75, [76, 175, 80]], // green
      [1.5, [255, 235, 59]], // yellow
      [2.25, [244, 67, 54]], // red
      [3, [156, 39, 176]], // purple
    ];
    const clamped = Math.min(Math.max(Number(v) || 0, 0), 3);
    for (let i = 0; i < stops.length - 1; i++) {
      const [v0, c0] = stops[i];
      const [v1, c1] = stops[i + 1];
      if (clamped <= v1) {
        const f = (clamped - v0) / (v1 - v0);
        const r = Math.round(c0[0] + (c1[0] - c0[0]) * f);
        const g = Math.round(c0[1] + (c1[1] - c0[1]) * f);
        const b = Math.round(c0[2] + (c1[2] - c0[2]) * f);
        return `rgb(${r},${g},${b})`;
      }
    }
    return `rgb(${stops[stops.length - 1][1].join(",")})`;
  }

  _render() {
    if (!this._hass || !this._entities) return;

    // hass.states is a single object HA replaces (and pushes to every card's
    // hass setter) on *any* entity changing anywhere, not just this zone's —
    // so without the guard below, this.innerHTML would get torn down and
    // rebuilt many times a minute. That guard alone isn't enough though: the
    // entities this card *does* watch (temperature, price tier, ...) still
    // update often enough to blow away whatever element the mouse happens to
    // be over mid-hover, resetting the browser's hover-tooltip timer before
    // it ever gets to fire. So on top of the guard, the constructor's
    // mouseenter/mouseleave listeners pause rendering entirely while the
    // pointer is anywhere over the card — see the _hovering check below —
    // and flush the latest state once the pointer leaves.
    if (this._hovering) {
      this._renderPending = true;
      return;
    }

    const watched = [
      this._entities.status,
      this._entities.compensated,
      this._entities.heatPumpOffset,
      this._entities.offset,
      this._entities.climate,
      this._entities.tier,
      this._entities.caution,
      this._entities.vacation,
    ].map((id) => this._state(id));
    const lang = pickLang(this._hass);
    const configKey = JSON.stringify(this._config);
    if (
      this._lastWatched &&
      watched.every((s, i) => s === this._lastWatched[i]) &&
      this._lastLang === lang &&
      this._lastConfigKey === configKey
    ) {
      return;
    }
    this._lastWatched = watched;
    this._lastLang = lang;
    this._lastConfigKey = configKey;

    const t = STRINGS[lang];
    const [status, compensated, heatPumpOffset, offset, climate, tier, caution, vacation] = watched;
    const attrs = (status && status.attributes) || {};
    const offsetAttrs = (offset && offset.attributes) || {};

    const active = attrs.active === true;
    const statusKey = status ? status.state : "error";
    const progress = Number(attrs.learning_progress_pct);
    const coverage = Number(attrs.outdoor_bands_covered_pct);
    // Exactly one of these two sensors is available at a time, chosen by the
    // configured output mode (see sensor.py) — the other reports unavailable
    // rather than a number that means nothing in that mode. Whichever is
    // live is "Publishing".
    const offsetMode = !!(heatPumpOffset && heatPumpOffset.state !== "unavailable");

    // When compensation is off, the published sensors fall back to the raw
    // outdoor temperature (see climate.py) instead of the number compensation
    // would have chosen — so preview that number from the status sensor's
    // recommended_* attributes instead, labeled "Would publish".
    // Clickable only while `active`: that's the one case where the number
    // shown is the entity's own state verbatim. Inactive, the cell shows
    // recommended_* from status's attributes instead (see comment above) —
    // the real entity is reporting the raw fallback, a different number, so
    // pointing history at it would show the wrong thing.
    const publishEntity = active ? (offsetMode ? this._entities.heatPumpOffset : this._entities.compensated) : null;
    const rows = [
      [
        active
          ? (offsetMode ? t.rowPublishingOffset : t.rowPublishing)
          : (offsetMode ? t.rowWouldPublishOffset : t.rowWouldPublish),
        active
          ? (offsetMode
              ? this._num(heatPumpOffset.state, 0, " °C")
              : this._num(compensated && compensated.state, 1, " °C"))
          : (offsetMode
              ? this._num(attrs.recommended_heat_pump_offset, 0, " °C")
                    : this._num(attrs.recommended_compensated_outdoor_temp_c, 1, " °C")),
              "explainPublishing",
              publishEntity,
      ],
                  [t.rowOutdoorNow, this._num(attrs.raw_outdoor_temp_c, 1, " °C"), "explainOutdoorNow"],
                  [t.rowIndoorNow, this._num(attrs.indoor_temp_c, 1, " °C"), "explainIndoorNow"],
                  [t.rowTarget, this._num(attrs.effective_indoor_target_c, 1, " °C"), "explainTarget"],
    ];

    // Absent rather than false, same as the sources chips below: these three
    // attributes are only published when their input is switched on, so a
    // missing key means the term is off — not that it's genuinely computing
    // to zero right now.
    const sunDisabled = attrs.cloud_sun_forecast_ok === undefined;
    const windDisabled = attrs.wind_forecast_ok === undefined;
    // price_ok reflects whether a price sensor is configured at all, not
    // whether compensation is switched on, so it can't tell us this term is
    // off. price_band_start is gated on the compensation toggle instead (see
    // sensor.py), and stays DISABLED regardless of price now/median, which
    // are the raw feed and read independently of this toggle — this flag is
    // reused below for the price section header badge.
    const priceDisabled = attrs.price_band_start === DISABLED;
    // Same trick, but the pre-ramp terms are numbers rather than absent when
    // off, so sensor.py substitutes DISABLED explicitly.
    const preRampDisabled = attrs.weather_preramp_c === DISABLED;
    const preCoolDisabled = attrs.sun_precool_c === DISABLED;

    // Pre-ramp (weather) and pre-cool (sun) are independent lookahead terms —
    // opposite signs, different lead times, each gated by its own toggle —
    // but they're shown as one combined box: a disabled term contributes 0 to
    // the sum rather than blanking the whole box, and the tooltip breaks the
    // two back out so which one is driving (or off) is never hidden.
    const preAdjustDisabled = preRampDisabled && preCoolDisabled;
    const rampDisplay = preRampDisabled ? t.badgeDisabled : this._num(attrs.weather_preramp_c, 2, " °C");
    const coolDisplay = preCoolDisabled ? t.badgeDisabled : this._num(attrs.sun_precool_c, 2, " °C");
    const preAdjustSum =
      (preRampDisabled ? 0 : Number(attrs.weather_preramp_c) || 0) +
      (preCoolDisabled ? 0 : Number(attrs.sun_precool_c) || 0);

    const terms = [
      [t.learnedOffset, this._num(offset && offset.state, 2, " °C"), "explainLearnedOffset", this._entities.offset],
      [t.termSun, sunDisabled ? DISABLED : this._num(attrs.sun_adjustment_c, 2, " °C"), "explainSun"],
      [t.termWind, windDisabled ? DISABLED : this._num(attrs.wind_adjustment_c, 2, " °C"), "explainWind"],
      [
        t.termPreAdjust,
        preAdjustDisabled ? DISABLED : this._num(preAdjustSum, 2, " °C"),
        "explainPreAdjust",
        null,
        { ramp: rampDisplay, cool: coolDisplay },
      ],
      [t.termPrice, priceDisabled ? DISABLED : this._num(attrs.price_adjustment_c, 2, " °C"), "explainPriceAdjustment"],
      [t.termTotalChange, this._num(attrs.total_adjustment_c, 2, " °C"), "explainTotalChange"],
    ];

    const learned = [
      [t.learnedOffset, this._num(offset && offset.state, 2, " °C"), "explainLearnedOffset", this._entities.offset],
      [t.outdoorBand, offsetAttrs.outdoor_band || "—", "explainOutdoorBand"],
      [t.respondsIn, this._num(attrs.response_time_min, 0, " min"), "explainRespondsIn"],
      [t.windsDownIn, this._num(attrs.wind_down_time_min, 0, " min"), "explainWindsDownIn"],
      [t.authorityLimit, this._num(attrs.authority_limit_c, 2, " °C"), "explainAuthorityLimit"],
      [t.settlingTime, this._num(attrs.settling_time_h, 1, " h"), "explainSettlingTime"],
    ];

    const priceAlways = [
      [t.priceNow, this._num(attrs.current_price, 2), "explainPriceNow"],
      [t.todaysMedian, this._num(attrs.price_median, 2), "explainTodaysMedian"],
    ];

    const price = [
      [t.brakesFrom, this._num(attrs.price_band_start, 2), "explainBrakesFrom"],
      [t.fullBrakeAt, this._num(attrs.price_band_full, 2), "explainFullBrakeAt"],
      [t.nextSpikeIn, this._num(attrs.upcoming_spike_in_min, 0, " min"), "explainNextSpikeIn"],
      [t.allowedSag, this._num(attrs.allowed_sag_c, 2, " °C"), "explainAllowedSag"],
      [t.coldBrake, this._num(attrs.cold_brake_factor, 2, "×"), "explainColdBrake"],
      [t.savingLevel, (tier && tier.state) || "—", "explainSavingLevel", this._entities.tier],
      [t.coldCaution, (caution && caution.state) || "—", "explainColdCaution", this._entities.caution],
      [t.preBrakeLead, this._num(attrs.lead_minutes_effective, 0, " min"), "explainPreBrakeLead"],
    ];

    // Absent rather than false: the status sensor only publishes a source's
    // health key when that source is switched on, so a missing key means the
    // term is off — not that it failed.
    const sources = [
      [t.sourceOutdoor, attrs.outdoor_sensor_ok],
      [t.sourceIndoor, attrs.indoor_sensor_ok],
      [t.sourceWind, attrs.wind_forecast_ok],
      [t.sourceCloudSun, attrs.cloud_sun_forecast_ok],
      [t.sourcePrice, attrs.price_ok],
    ].map(([label, ok]) => {
      const cls = ok === undefined ? "co-off" : ok ? "co-good" : "co-bad";
      const text = ok === undefined ? t.srcOff : ok ? t.srcOk : t.srcUnavailable;
      return `<span class="co-chip ${cls}"${this._tooltip(t, "explainSource")}>${this._esc(label)}<b>${this._esc(text)}</b></span>`;
    });

    const notes = [];
    // Surfaced here so a target that looks unusually low is explained without
    // needing to open the separate truetemp-vacation-card.js. "inactive" and
    // "done" mean nothing is currently sagging the target, so those two phases
    // add no note (see VACATION_NOTE_KEYS).
    const vacationPhase = vacation ? vacation.state : null;
    const vacationNoteKey = vacationPhase && VACATION_NOTE_KEYS[vacationPhase];
    if (vacationNoteKey) {
      const vacationAttrs = vacation.attributes || {};
      const value =
        vacationPhase === "invalid"
          ? vacationAttrs.reason || ""
          : vacationPhase === "scheduled"
            ? this._fmtDateTime(vacationAttrs.start_at)
            : this._fmtDateTime(vacationAttrs.return_at);
      notes.push(fmt(t[vacationNoteKey], { value }));
    }
    if (!active) {
      notes.push(offsetMode ? t.noteCompOffOffset : t.noteCompOffRaw);
    }
    if (attrs.learning_paused && attrs.learning_paused_because) {
      notes.push(fmt(t.noteLearningPaused, { value: attrs.learning_paused_because }));
    }
    if (attrs.pump_at_capacity) {
      notes.push(t.notePumpAtCapacity);
    }
    if (attrs.response_time_measured === false) {
      notes.push(t.noteResponseNotMeasured);
    }
    if (attrs.heating_hard_limit_engaged) {
      notes.push(t.noteHeatingHardLimit);
    }
    if (attrs.precharge_active) {
      notes.push(t.notePrecharge);
    } else if (attrs.price_braking) {
      notes.push(t.notePriceBraking);
    }
    if (attrs.last_error) {
      const since = attrs.last_error_at ? ` (${new Date(attrs.last_error_at).toLocaleTimeString()})` : "";
      notes.push(fmt(t.noteLastError, { value: attrs.last_error + since }));
    }

    const title =
      (climate && climate.attributes.friendly_name) || "TrueTemp";
    const showBands = this._config.bands !== false;
    const showPrice = this._config.price !== false;
    const showPriceGraph = this._config.priceGraph !== false;
    const showSources = this._config.sources !== false;

    this.innerHTML = `
      <ha-card header="${this._esc(title)}">
        <div class="co-body">
          <div class="co-status co-${this._esc(statusKey)} co-clickable" data-entity="${this._esc(this._entities.status)}" role="button" tabindex="0"${this._tooltip(t, "explainStatus")}>
            <span class="co-dot"></span>
            <span>${this._esc(t["status" + statusKey.charAt(0).toUpperCase() + statusKey.slice(1)] || statusKey)}</span>
            ${active ? "" : `<span class="co-badge">${this._esc(t.badgeOff)}</span>`}
          </div>

          <div class="co-grid">${this._cells(rows, t)}</div>

          <div class="co-section">${this._esc(t.sectionAddedThisCycle)}</div>
          <div class="co-grid">${this._cells(terms, t)}</div>

          <div class="co-section">${this._esc(t.sectionPrice)}${showPrice && priceDisabled ? ` <span class="co-badge">${this._esc(t.badgeDisabled)}</span>` : ""}</div>
          <div class="co-grid${showPrice ? "" : " co-grid-compact"}">${this._cells(showPrice ? [...priceAlways, ...price] : priceAlways, t)}</div>

          ${showPriceGraph ? this._priceGraph(attrs, t) : ""}

          <div class="co-section">${this._esc(t.sectionHouseKnowledge)}</div>
          <div class="co-bar-group">
            <div class="co-bar co-bar-coverage"${this._tooltip(t, "coverageBarTitle")}><div class="co-fill co-fill-coverage" style="width:${Number.isNaN(coverage) ? 0 : coverage}%"></div></div>
            <div class="co-bar"${this._tooltip(t, "progressBarTitle")}><div class="co-fill" style="width:${Number.isNaN(progress) ? 0 : progress}%"></div></div>
          </div>
          <div class="co-meta">${fmt(t.metaLine, { progress: Number.isNaN(progress) ? "—" : progress, coverage: Number.isNaN(coverage) ? "—" : coverage })}</div>
          <div class="co-grid">${this._cells(learned, t)}</div>

          ${showBands ? this._bandTable(offsetAttrs, t) : ""}

          ${showSources ? `<div class="co-section">${this._esc(t.sectionSources)}</div>
          <div class="co-chips">${sources.join("")}</div>` : ""}

          ${notes.length ? `<div class="co-notes">${notes.map((n) => `<div>${this._esc(n)}</div>`).join("")}</div>` : ""}
          ${attrs.reason ? `<div class="co-reason">${this._esc(attrs.reason)}</div>` : ""}
        </div>
      </ha-card>
      <style>
        .co-body { padding: 0 16px 16px; }
        .co-status { display:flex; align-items:center; gap:8px; font-size:14px; margin-bottom:12px; }
        .co-dot { width:9px; height:9px; border-radius:50%; background: var(--success-color, #4c1); flex:none; }
        .co-degraded .co-dot { background: var(--warning-color, #fa3); }
        .co-error .co-dot { background: var(--error-color, #d33); }
        .co-badge { margin-left:auto; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
                    padding:2px 7px; border-radius:3px; background: var(--secondary-background-color);
                    color: var(--secondary-text-color); }
        .co-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px; }
        /* Capped at 220px so a short row (e.g. the 2-cell price-now row) can't
           stretch its cells far past the size of cells in the fuller rows
           around it. */
        .co-grid-compact { grid-template-columns:repeat(auto-fill,minmax(120px,220px)); }
        .co-cell { display:flex; flex-direction:column; gap:2px; padding:8px 10px; border-radius:4px;
                   background: var(--secondary-background-color);
                   border:1px solid var(--divider-color); }
        .co-cell span { font-size:11px; color: var(--secondary-text-color); }
        .co-cell b { font-size:16px; font-weight:500; font-variant-numeric: tabular-nums; }
        .co-cell.co-off b { font-size:12px; font-style:italic; color: var(--disabled-text-color, #999); }
        .co-clickable { cursor:pointer; }
        .co-cell.co-clickable:hover, .co-cell.co-clickable:focus-visible { background: var(--divider-color); }
        .co-status.co-clickable:hover, .co-status.co-clickable:focus-visible { text-decoration: underline; }
        .co-clickable:focus-visible { outline: 2px solid var(--primary-color); outline-offset:1px; }
        .co-section { margin:18px 0 8px; font-size:11px; text-transform:uppercase; letter-spacing:.08em;
                      color: var(--secondary-text-color); }
        .co-bar-group { display:flex; flex-direction:column; gap:3px; }
        .co-bar { height:5px; border-radius:3px; background: var(--divider-color); overflow:hidden; }
        .co-fill { height:100%; background: var(--primary-color); transition: width .4s ease; }
        .co-fill-coverage { background: var(--success-color, #4c1); }
        .co-meta { font-size:11px; color: var(--secondary-text-color); margin:6px 0 10px; }
        /* Five columns will not fit a phone; scroll the table, never the page. */
        .co-scroll { overflow-x:auto; }
        .co-table { width:100%; border-collapse:collapse; font-size:12px;
                    font-variant-numeric: tabular-nums; white-space:nowrap; }
        .co-table th { text-align:right; font-weight:500; padding:4px 8px;
                       color: var(--secondary-text-color); border-bottom:1px solid var(--divider-color); }
        .co-table td { text-align:right; padding:4px 8px;
                       border-bottom:1px solid var(--divider-color); }
        .co-table th:first-child, .co-table td:first-child { text-align:left; }
        .co-table tr.co-empty td { color: var(--disabled-text-color, #999); }
        .co-table tr.co-ok td { color: var(--success-color, #4c1); }
        .co-samples-done { color: var(--success-color, #4c1); font-weight:700; }
        .co-table tr.co-now { background: var(--secondary-background-color); }
        .co-table tr.co-now td { font-weight:600; }
        .co-spill { margin-left:4px; font-style:normal; color: var(--secondary-text-color); cursor:help; }
        .co-legend { margin-top:6px; font-size:11px; line-height:1.5; color: var(--secondary-text-color); }
        .co-chips { display:flex; flex-wrap:wrap; gap:6px; }
        .co-chip { display:flex; align-items:center; gap:5px; font-size:11px; padding:3px 8px;
                   border-radius:10px; background: var(--secondary-background-color);
                   color: var(--secondary-text-color); }
        .co-chip b { font-weight:600; }
        .co-chip.co-good b { color: var(--success-color, #4c1); }
        .co-chip.co-bad b { color: var(--error-color, #d33); }
        .co-chip.co-off b { color: var(--disabled-text-color, #999); font-weight:400; font-style:italic; }
        .co-pricegraph svg { width:100%; height:70px; display:block; }
        .co-pg-now { stroke: var(--primary-text-color); stroke-width:1.5; stroke-dasharray:3,2; }
        .co-pg-legend { display:flex; align-items:center; gap:6px; margin-top:6px; font-size:11px; color: var(--secondary-text-color); }
        .co-pg-swatch { width:9px; height:9px; border-radius:2px; display:inline-block; margin-left:8px; }
        .co-pg-swatch:first-child { margin-left:0; }
        .co-notes { margin-top:14px; font-size:12px; line-height:1.5; color: var(--primary-text-color);
                    border-left:3px solid var(--warning-color, #fa3); padding-left:10px; }
        .co-reason { margin-top:14px; font-size:11px; line-height:1.5; color: var(--secondary-text-color); }
        @media (prefers-reduced-motion: reduce) { .co-fill { transition: none; } }
      </style>
    `;
  }
}

customElements.define("truetemp-card", TrueTempCard);

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "truetemp" } } },
  { name: "bands", selector: { boolean: {} } },
  { name: "price", selector: { boolean: {} } },
  { name: "priceGraph", selector: { boolean: {} } },
  { name: "sources", selector: { boolean: {} } },
];

// Thin wrapper around ha-form: the card's config is flat enough (one required
// entity, three optional toggles) that a hand-rolled form would just be a
// worse copy of what HA already renders for every other card.
class TrueTempCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { bands: true, price: true, priceGraph: true, sources: true, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const t = STRINGS[pickLang(this._hass)];
    const labels = {
      entity: t.editorEntity,
      bands: t.editorBands,
      price: t.editorPrice,
      priceGraph: t.editorPriceGraph,
      sources: t.editorSources,
    };
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        this._config = ev.detail.value;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config } }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = EDITOR_SCHEMA;
    this._form.computeLabel = (schema) => labels[schema.name] || schema.name;
  }
}

customElements.define("truetemp-card-editor", TrueTempCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "truetemp-card",
  name: "TrueTemp",
  description: "What TrueTemp is doing, and how well it knows your house.",
});
