// TrueTemp vacation plans card.
//
// Ships as a second, independent custom element from truetemp-card.js on
// purpose — small files, load-size isolation, same reasoning as
// truetemp-card.js's own header comment. This one edits a *list* of plans
// rather than a single scenario, so on top of the master arm switch it also
// renders the plan list read from
// sensor.<name>_vacation_status's `plans` attribute and writes individual
// plans through the truetemp.vacation_plan_set/_remove/_reorder services
// (see services.py) — there is no other way to persist a plan from outside
// a config-flow step.
//
// Add it with:
//     type: custom:truetemp-vacation-card
//     entity: sensor.<name>_vacation_status
//
// Any entity belonging to the zone works — the rest (the arm switch, the
// config entry to address in service calls) are discovered through the
// frontend entity registry, so renaming entities never breaks it.

import { STRINGS as EN } from "./lang/en.js";
import { STRINGS as DE } from "./lang/de.js";
import { STRINGS as SV } from "./lang/sv.js";

const TRANSLATION_KEYS = {
  status: "vacation_status",
  armed: "vacation_mode",
};

const SUFFIXES = {
  status: "vacation_status",
  armed: "vacation_mode",
};

const STRINGS = {
  en: EN,
  de: { ...EN, ...DE },
  sv: { ...EN, ...SV },
};

function pickLang(hass) {
  const raw = (hass && (hass.language || (hass.locale && hass.locale.language))) || "en";
  const base = String(raw).split("-")[0].toLowerCase();
  return STRINGS[base] ? base : "en";
}

// Same six-state phase set as holiday.py's HolidayResult — reuse the
// holiday card's already-translated phase labels rather than duplicating
// them under a new key.
const PHASE_KEYS = {
  inactive: "holidayPhaseInactive",
  invalid: "holidayPhaseInvalid",
  scheduled: "holidayPhaseScheduled",
  setback: "holidayPhaseSetback",
  ramping: "holidayPhaseRamping",
  recovering: "holidayPhaseRecovering",
  done: "holidayPhaseDone",
};

const OFF_TRACK_PHASES = new Set(["scheduled", "setback", "ramping", "recovering"]);

const RECURRENCE_ONCE = "once";
const RECURRENCE_WEEKLY = "weekly";
const RECURRENCE_YEARLY = "yearly";

const WEEKDAY_KEYS = [
  "vacationWeekdayMonday",
  "vacationWeekdayTuesday",
  "vacationWeekdayWednesday",
  "vacationWeekdayThursday",
  "vacationWeekdayFriday",
  "vacationWeekdaySaturday",
  "vacationWeekdaySunday",
];

// Fields service calls carry, per recurrence — used both to build a service
// payload from form state and to strip an existing plan dict down to the
// fields relevant to its own recurrence before resending it (e.g. the
// enabled-toggle and reorder writes, which resend a plan unchanged apart
// from one field).
const RECURRENCE_FIELDS = {
  [RECURRENCE_ONCE]: ["start_date", "end_date"],
  [RECURRENCE_WEEKLY]: ["start_weekday", "start_time", "stop_weekday", "stop_time"],
  [RECURRENCE_YEARLY]: ["start_month", "start_day", "end_month", "end_day"],
};

class TrueTempVacationCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error(STRINGS[pickLang(this._hass)].vacationSetEntityError);
    }
    this._config = config;
    this._entities = null;
    this._built = false;
    this._formState = null;
    this._formError = null;
    this._confirmingDeleteId = null;
    this._plansById = {};
  }

  getCardSize() {
    return 4 + Object.keys(this._plansById).length;
  }

  static getConfigElement() {
    return document.createElement("truetemp-vacation-card-editor");
  }

  static getStubConfig(hass) {
    const entityId = Object.keys(hass.entities || {}).find(
      (id) => hass.entities[id].platform === "truetemp" && id.endsWith("_vacation_status"),
    );
    return { entity: entityId || "" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entities || !this._entities.configEntryId) {
      this._entities = this._discover(hass);
    }
    this._render();
  }

  // Find the zone's arm switch and this device's config entry via the
  // device the configured status entity belongs to — see truetemp-card.js's
  // own comment for why translation_key, not entity_id, is matched first.
  _discover(hass) {
    const found = { armed: null, configEntryId: null };

    const configured = this._config.entity;
    const entry = hass.entities && hass.entities[configured];
    const deviceId = entry && entry.device_id;
    // hass.entities is the frontend's compact entity registry — it carries
    // device_id but not config_entry_id. The device registry does, via
    // config_entries; a TrueTemp device belongs to exactly one config entry.
    const device = deviceId && hass.devices && hass.devices[deviceId];
    found.configEntryId = (device && device.config_entries && device.config_entries[0]) || null;
    if (!deviceId || !hass.entities) {
      found.status = configured;
      return found;
    }

    Object.keys(hass.entities).forEach((entityId) => {
      const info = hass.entities[entityId];
      if (info.device_id !== deviceId) return;
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

  _esc(value) {
    return String(value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  _fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  // Date-only ISO strings ("2026-08-20") parsed as a local calendar date,
  // not UTC midnight — `new Date("2026-08-20")` shifts a day in negative-UTC
  // offset timezones when formatted with toLocaleDateString.
  _fmtDate(iso) {
    if (!iso) return "—";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return "—";
    return new Date(y, m - 1, d).toLocaleDateString();
  }

  _fmtTime(iso) {
    return iso ? iso.slice(0, 5) : "—";
  }

  _fmtMonthDay(month, day) {
    if (!month || !day) return "—";
    return new Date(2000, month - 1, day).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  }

  _recurrenceSummary(t, plan) {
    if (plan.recurrence === RECURRENCE_ONCE) {
      return `${this._fmtDate(plan.start_date)} → ${this._fmtDate(plan.end_date)}`;
    }
    if (plan.recurrence === RECURRENCE_WEEKLY) {
      const startDay = t[WEEKDAY_KEYS[plan.start_weekday]] ?? plan.start_weekday;
      const stopDay = t[WEEKDAY_KEYS[plan.stop_weekday]] ?? plan.stop_weekday;
      return `${startDay} ${this._fmtTime(plan.start_time)} → ${stopDay} ${this._fmtTime(plan.stop_time)}`;
    }
    if (plan.recurrence === RECURRENCE_YEARLY) {
      return `${this._fmtMonthDay(plan.start_month, plan.start_day)} → ${this._fmtMonthDay(plan.end_month, plan.end_day)}`;
    }
    return "—";
  }

  // Builds the static, list-independent shell once (and again only on a
  // language change). The plan list and the add/edit form are rebuilt
  // separately, see `_renderPlansList`/`_renderForm`.
  _build(t) {
    this.innerHTML = `
      <ha-card header="${this._esc(t.vacationCardTitle)}">
        <div class="va-body">
          <div class="va-row">
            <label>${this._esc(t.vacationLabelArmed)}</label>
            <label class="va-switch">
              <input type="checkbox" class="va-armed">
              <span class="va-slider"></span>
            </label>
          </div>
          <div class="va-status"><span class="va-dot"></span><span class="va-phase"></span></div>
          <div class="va-grid">
            <div class="va-cell"><span>${this._esc(t.holidayRowStartAt)}</span><b class="va-start-at">—</b></div>
            <div class="va-cell"><span>${this._esc(t.holidayRowBackBy)}</span><b class="va-return-at">—</b></div>
          </div>
          <div class="va-off-track" hidden></div>
          <div class="va-reason"></div>

          <div class="va-plans-header">
            <span>${this._esc(t.vacationLabelPlans)}</span>
            <button type="button" class="va-add-btn">${this._esc(t.vacationAddPlan)}</button>
          </div>
          <div class="va-plans-list"></div>
          <div class="va-form" hidden></div>
        </div>
      </ha-card>
      <style>
        .va-body { padding: 0 16px 16px; display:flex; flex-direction:column; gap:10px; }
        .va-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
        .va-row label { font-size:13px; color: var(--primary-text-color); }
        .va-switch, .va-mini-switch { position:relative; display:inline-block; width:40px; height:22px; flex:none; }
        .va-switch input, .va-mini-switch input { opacity:0; width:0; height:0; }
        .va-slider, .va-mini-switch span { position:absolute; inset:0; background: var(--divider-color); border-radius:22px;
                     transition:.15s; cursor:pointer; }
        .va-slider::before, .va-mini-switch span::before { content:""; position:absolute; width:16px; height:16px; left:3px; top:3px;
                              background:#fff; border-radius:50%; transition:.15s; }
        .va-switch input:checked + .va-slider, .va-mini-switch input:checked + span { background: var(--primary-color); }
        .va-switch input:checked + .va-slider::before, .va-mini-switch input:checked + span::before { transform: translateX(18px); }
        .va-status { display:flex; align-items:center; gap:8px; font-size:14px; margin-top:4px; }
        .va-dot { width:9px; height:9px; border-radius:50%; background: var(--disabled-text-color, #999); flex:none; }
        .va-setback .va-dot { background: var(--warning-color, #fa3); }
        .va-ramping .va-dot { background: var(--info-color, #39a); }
        .va-recovering .va-dot { background: var(--info-color, #39a); }
        .va-scheduled .va-dot, .va-done .va-dot { background: var(--success-color, #4c1); }
        .va-invalid .va-dot { background: var(--error-color, #d33); }
        .va-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:8px; }
        .va-cell { display:flex; flex-direction:column; gap:2px; padding:8px 10px; border-radius:4px;
                   background: var(--secondary-background-color); border:1px solid var(--divider-color); }
        .va-cell span { font-size:11px; color: var(--secondary-text-color); }
        .va-cell b { font-size:14px; font-weight:500; font-variant-numeric: tabular-nums; }
        .va-off-track { font-size:12px; line-height:1.5; color: var(--primary-text-color);
                         border-left:3px solid var(--warning-color, #fa3); padding-left:10px; }
        .va-reason { font-size:11px; line-height:1.5; color: var(--secondary-text-color); }
        .va-plans-header { display:flex; align-items:center; justify-content:space-between; margin-top:6px; }
        .va-plans-header span { font-size:13px; font-weight:500; color: var(--primary-text-color); }
        button { font: inherit; cursor:pointer; }
        .va-add-btn, .va-form button, .va-plan-actions button {
          color: var(--primary-color); background:none; border:1px solid var(--divider-color);
          border-radius:4px; padding:4px 10px; font-size:12px;
        }
        .va-plan-actions button.va-danger, .va-form button.va-danger { color: var(--error-color, #d33); }
        .va-plans-list { display:flex; flex-direction:column; gap:6px; }
        .va-plans-list[hidden] { display:none; }
        .va-plan-row { display:flex; flex-direction:column; gap:6px; padding:8px 10px; border-radius:4px;
                       background: var(--secondary-background-color); border:1px solid var(--divider-color); }
        .va-plan-row.va-active { border-color: var(--primary-color); }
        .va-plan-main { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-size:13px; }
        .va-plan-name { font-weight:500; color: var(--primary-text-color); }
        .va-plan-summary, .va-plan-temp { color: var(--secondary-text-color); font-size:12px; }
        .va-plan-badge { font-size:10px; padding:1px 6px; border-radius:8px; background: var(--primary-color); color:#fff; }
        .va-plan-actions { display:flex; align-items:center; gap:6px; }
        .va-empty { font-size:12px; color: var(--secondary-text-color); }
        .va-form { display:flex; flex-direction:column; gap:10px; border-top:1px solid var(--divider-color); padding-top:10px; }
        .va-form[hidden] { display:none; }
        .va-field { display:flex; flex-direction:column; gap:3px; }
        .va-field label { font-size:12px; color: var(--secondary-text-color); }
        .va-field input, .va-field select {
          font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
          border: 1px solid var(--divider-color); border-radius:4px; padding:6px 8px;
        }
        .va-field-row { display:flex; gap:10px; }
        .va-field-row .va-field { flex:1; }
        .va-form-actions { display:flex; justify-content:flex-end; gap:8px; }
        .va-form-error { font-size:12px; color: var(--error-color, #d33); }
      </style>
    `;

    const els = {
      armed: this.querySelector(".va-armed"),
      status: this.querySelector(".va-status"),
      phase: this.querySelector(".va-phase"),
      startAt: this.querySelector(".va-start-at"),
      returnAt: this.querySelector(".va-return-at"),
      offTrack: this.querySelector(".va-off-track"),
      reason: this.querySelector(".va-reason"),
      addBtn: this.querySelector(".va-add-btn"),
      plansList: this.querySelector(".va-plans-list"),
      form: this.querySelector(".va-form"),
    };

    els.armed.addEventListener("change", (ev) => this._setArmed(ev.target.checked));
    els.addBtn.addEventListener("click", () => this._openForm(null));

    els.plansList.addEventListener("click", (ev) => this._onPlansListClick(ev));
    els.plansList.addEventListener("change", (ev) => this._onPlansListChange(ev));

    this._els = els;
    this._built = true;
  }

  _setArmed(on) {
    const entityId = this._entities.armed;
    if (!entityId || !this._hass) return;
    this._hass.callService("switch", on ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  _planById(id) {
    return this._plansById[id] || null;
  }

  // Strips a plan dict down to the fields its own recurrence needs, plus
  // the common fields — the shape truetemp.vacation_plan_set expects.
  // Applied both to fresh form state and to an existing plan being resent
  // unchanged apart from one field (enabled toggle, reorder).
  _planToServiceFields(plan) {
    const fields = {
      id: plan.id,
      name: plan.name,
      enabled: plan.enabled,
      recurrence: plan.recurrence,
      min_temp_c: plan.min_temp_c,
    };
    for (const key of RECURRENCE_FIELDS[plan.recurrence] || []) {
      // An unfilled field is `null`/`undefined` here (fresh form default, or
      // a field left over from a different recurrence) — omit it rather than
      // sending it literally, so the service sees a missing key and raises
      // its friendly "missing field" error instead of a raw schema-validation
      // one (voluptuous's cv.string rejects `None` before the handler runs).
      if (plan[key] === null || plan[key] === undefined) continue;
      fields[key] = plan[key];
    }
    return fields;
  }

  async _callVacationService(service, data) {
    const t = STRINGS[pickLang(this._hass)];
    if (!this._entities.configEntryId || !this._hass) {
      throw new Error(t.vacationErrorGeneric);
    }
    return this._hass.callService("truetemp", service, {
      config_entry_id: this._entities.configEntryId,
      ...data,
    });
  }

  async _toggleEnabled(id, enabled) {
    const plan = this._planById(id);
    if (!plan) return;
    try {
      await this._callVacationService("vacation_plan_set", this._planToServiceFields({ ...plan, enabled }));
    } catch (err) {
      // Refused (e.g. would overlap another enabled plan) — re-render to
      // snap the checkbox back to the last known server state.
      this._render();
    }
  }

  async _reorderPlan(id, direction) {
    try {
      await this._callVacationService("vacation_plan_reorder", { id, direction });
    } catch (err) {
      this._render();
    }
  }

  async _deletePlan(id) {
    this._confirmingDeleteId = null;
    try {
      await this._callVacationService("vacation_plan_remove", { id });
    } catch (err) {
      this._render();
    }
  }

  _openForm(plan) {
    this._formError = null;
    this._formState = plan
      ? { ...plan }
      : {
          id: null,
          name: "",
          enabled: true,
          recurrence: RECURRENCE_ONCE,
          min_temp_c: 15.0,
          start_date: null,
          end_date: null,
          start_weekday: 0,
          start_time: "18:00",
          stop_weekday: 0,
          stop_time: "07:00",
          start_month: 1,
          start_day: 1,
          end_month: 1,
          end_day: 14,
        };
    this._render();
  }

  _closeForm() {
    this._formState = null;
    this._formError = null;
    this._render();
  }

  async _submitForm() {
    const t = STRINGS[pickLang(this._hass)];
    try {
      const fields = this._planToServiceFields(this._formState);
      if (!fields.id) delete fields.id;
      await this._callVacationService("vacation_plan_set", fields);
      this._closeForm();
    } catch (err) {
      this._formError = (err && err.message) || t.vacationErrorGeneric;
      this._renderForm(t);
    }
  }

  _onPlansListClick(ev) {
    const btn = ev.target.closest("button[data-action]");
    if (!btn) return;
    const row = btn.closest(".va-plan-row");
    const id = row && row.dataset.id;
    const action = btn.dataset.action;
    if (action === "edit") this._openForm(this._planById(id));
    else if (action === "delete") {
      this._confirmingDeleteId = id;
      this._renderPlansList(STRINGS[pickLang(this._hass)]);
    } else if (action === "delete-cancel") {
      this._confirmingDeleteId = null;
      this._renderPlansList(STRINGS[pickLang(this._hass)]);
    } else if (action === "delete-confirm") this._deletePlan(id);
    else if (action === "up") this._reorderPlan(id, "up");
    else if (action === "down") this._reorderPlan(id, "down");
  }

  _onPlansListChange(ev) {
    if (!ev.target.classList.contains("va-plan-enabled")) return;
    const row = ev.target.closest(".va-plan-row");
    this._toggleEnabled(row.dataset.id, ev.target.checked);
  }

  _renderPlansList(t) {
    const attrs = this._lastAttrs || {};
    const plans = attrs.plans || [];
    this._plansById = {};
    plans.forEach((p) => {
      this._plansById[p.id] = p;
    });

    if (!plans.length) {
      this._els.plansList.innerHTML = `<div class="va-empty">${this._esc(t.vacationEmptyList)}</div>`;
      return;
    }

    this._els.plansList.innerHTML = plans
      .map((plan, index) => {
        const active = plan.id === attrs.active_plan_id;
        const confirming = this._confirmingDeleteId === plan.id;
        const deleteButtons = confirming
          ? `<button type="button" class="va-danger" data-action="delete-confirm">${this._esc(t.vacationDeleteConfirm)}</button>
             <button type="button" data-action="delete-cancel">${this._esc(t.vacationCancel)}</button>`
          : `<button type="button" class="va-danger" data-action="delete">${this._esc(t.vacationDeletePlan)}</button>`;
        return `
          <div class="va-plan-row${active ? " va-active" : ""}" data-id="${this._esc(plan.id)}">
            <div class="va-plan-main">
              <span class="va-plan-name">${this._esc(plan.name)}</span>
              <span class="va-plan-summary">${this._esc(this._recurrenceSummary(t, plan))}</span>
              <span class="va-plan-temp">${this._esc(plan.min_temp_c)}°C</span>
              ${active ? `<span class="va-plan-badge">${this._esc(t.vacationActiveBadge)}</span>` : ""}
            </div>
            <div class="va-plan-actions">
              <button type="button" data-action="up" ${index === 0 ? "disabled" : ""}>${this._esc(t.vacationMoveUp)}</button>
              <button type="button" data-action="down" ${index === plans.length - 1 ? "disabled" : ""}>${this._esc(t.vacationMoveDown)}</button>
              <label class="va-mini-switch">
                <input type="checkbox" class="va-plan-enabled" ${plan.enabled ? "checked" : ""}>
                <span></span>
              </label>
              <button type="button" data-action="edit">${this._esc(t.vacationEditPlan)}</button>
              ${deleteButtons}
            </div>
          </div>
        `;
      })
      .join("");
  }

  _weekdaySelect(t, selected, cls) {
    const options = WEEKDAY_KEYS.map(
      (key, i) => `<option value="${i}" ${i === selected ? "selected" : ""}>${this._esc(t[key])}</option>`,
    ).join("");
    return `<select class="${cls}">${options}</select>`;
  }

  _renderForm(t) {
    const state = this._formState;
    if (!state) {
      this._els.form.hidden = true;
      this._els.plansList.hidden = false;
      this._els.addBtn.hidden = false;
      return;
    }
    this._els.plansList.hidden = true;
    this._els.addBtn.hidden = true;
    this._els.form.hidden = false;

    const recurrenceFieldsHtml = {
      [RECURRENCE_ONCE]: `
        <div class="va-field-row">
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStartDate)}</label>
            <input type="date" class="va-f-start_date" value="${state.start_date || ""}">
          </div>
          <div class="va-field">
            <label>${this._esc(t.vacationFieldEndDate)}</label>
            <input type="date" class="va-f-end_date" value="${state.end_date || ""}">
          </div>
        </div>
      `,
      [RECURRENCE_WEEKLY]: `
        <div class="va-field-row">
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStartWeekday)}</label>
            ${this._weekdaySelect(t, state.start_weekday, "va-f-start_weekday")}
          </div>
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStartTime)}</label>
            <input type="time" class="va-f-start_time" value="${state.start_time || ""}">
          </div>
        </div>
        <div class="va-field-row">
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStopWeekday)}</label>
            ${this._weekdaySelect(t, state.stop_weekday, "va-f-stop_weekday")}
          </div>
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStopTime)}</label>
            <input type="time" class="va-f-stop_time" value="${state.stop_time || ""}">
          </div>
        </div>
      `,
      [RECURRENCE_YEARLY]: `
        <div class="va-field-row">
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStartMonth)}</label>
            <input type="number" min="1" max="12" class="va-f-start_month" value="${state.start_month ?? ""}">
          </div>
          <div class="va-field">
            <label>${this._esc(t.vacationFieldStartDay)}</label>
            <input type="number" min="1" max="31" class="va-f-start_day" value="${state.start_day ?? ""}">
          </div>
        </div>
        <div class="va-field-row">
          <div class="va-field">
            <label>${this._esc(t.vacationFieldEndMonth)}</label>
            <input type="number" min="1" max="12" class="va-f-end_month" value="${state.end_month ?? ""}">
          </div>
          <div class="va-field">
            <label>${this._esc(t.vacationFieldEndDay)}</label>
            <input type="number" min="1" max="31" class="va-f-end_day" value="${state.end_day ?? ""}">
          </div>
        </div>
      `,
    };

    this._els.form.innerHTML = `
      <div class="va-field">
        <label>${this._esc(t.vacationFieldName)}</label>
        <input type="text" class="va-f-name" value="${this._esc(state.name)}">
      </div>
      <div class="va-field-row">
        <div class="va-field">
          <label>${this._esc(t.vacationFieldRecurrence)}</label>
          <select class="va-f-recurrence">
            <option value="${RECURRENCE_ONCE}" ${state.recurrence === RECURRENCE_ONCE ? "selected" : ""}>${this._esc(t.vacationRecurrenceOnce)}</option>
            <option value="${RECURRENCE_WEEKLY}" ${state.recurrence === RECURRENCE_WEEKLY ? "selected" : ""}>${this._esc(t.vacationRecurrenceWeekly)}</option>
            <option value="${RECURRENCE_YEARLY}" ${state.recurrence === RECURRENCE_YEARLY ? "selected" : ""}>${this._esc(t.vacationRecurrenceYearly)}</option>
          </select>
        </div>
        <div class="va-field">
          <label>${this._esc(t.vacationFieldMinTemp)}</label>
          <input type="number" step="0.5" class="va-f-min_temp_c" value="${state.min_temp_c}">
        </div>
      </div>
      <div class="va-field">
        <label class="va-switch">
          <input type="checkbox" class="va-f-enabled" ${state.enabled ? "checked" : ""}>
          <span class="va-slider"></span>
        </label>
        <label>${this._esc(t.vacationFieldEnabled)}</label>
      </div>
      <div class="va-recurrence-fields">${recurrenceFieldsHtml[state.recurrence] || ""}</div>
      ${this._formError ? `<div class="va-form-error">${this._esc(this._formError)}</div>` : ""}
      <div class="va-form-actions">
        <button type="button" class="va-cancel">${this._esc(t.vacationCancel)}</button>
        <button type="button" class="va-save">${this._esc(t.vacationSave)}</button>
      </div>
    `;

    const bindText = (cls, key, parse) => {
      const el = this._els.form.querySelector(`.${cls}`);
      if (!el) return;
      el.addEventListener("change", (ev) => {
        state[key] = parse ? parse(ev.target.value) : ev.target.value;
      });
    };
    bindText("va-f-name", "name");
    bindText("va-f-min_temp_c", "min_temp_c", Number);
    bindText("va-f-start_date", "start_date");
    bindText("va-f-end_date", "end_date");
    bindText("va-f-start_weekday", "start_weekday", Number);
    bindText("va-f-start_time", "start_time");
    bindText("va-f-stop_weekday", "stop_weekday", Number);
    bindText("va-f-stop_time", "stop_time");
    bindText("va-f-start_month", "start_month", Number);
    bindText("va-f-start_day", "start_day", Number);
    bindText("va-f-end_month", "end_month", Number);
    bindText("va-f-end_day", "end_day", Number);

    this._els.form.querySelector(".va-f-enabled").addEventListener("change", (ev) => {
      state.enabled = ev.target.checked;
    });
    this._els.form.querySelector(".va-f-recurrence").addEventListener("change", (ev) => {
      state.recurrence = ev.target.value;
      this._renderForm(t);
    });
    this._els.form.querySelector(".va-cancel").addEventListener("click", () => this._closeForm());
    this._els.form.querySelector(".va-save").addEventListener("click", () => this._submitForm());
  }

  _render() {
    if (!this._hass || !this._entities) return;
    const lang = pickLang(this._hass);
    const t = STRINGS[lang];
    if (!this._built || this._lastLang !== lang) {
      this._build(t);
      this._lastLang = lang;
    }

    const armed = this._state(this._entities.armed);
    const status = this._state(this._entities.status);
    const attrs = (status && status.attributes) || {};
    this._lastAttrs = attrs;

    const els = this._els;
    const focused = this.contains(document.activeElement) ? document.activeElement : null;

    if (els.armed !== focused) {
      els.armed.checked = !!(armed && armed.state === "on");
    }

    const phaseKey = status ? status.state : "inactive";
    els.status.className = `va-status va-${this._esc(phaseKey)}`;
    els.phase.textContent = t[PHASE_KEYS[phaseKey]] || phaseKey;

    els.startAt.textContent = this._fmtDateTime(attrs.start_at);
    els.returnAt.textContent = this._fmtDateTime(attrs.return_at);

    const offTrack = attrs.on_track === false && OFF_TRACK_PHASES.has(phaseKey);
    els.offTrack.hidden = !offTrack;
    els.offTrack.textContent = offTrack ? t.holidayNoteOffTrack : "";

    els.reason.textContent = attrs.reason || "";

    // The add/edit form is local, user-driven state — never rebuilt by a
    // hass-triggered refresh while open, so an in-progress edit is never
    // blown away by a coordinator cycle landing mid-edit. Only the plan
    // list (read-only rows) is refreshed on every render.
    if (this._formState) {
      if (!this._formBuiltFor || this._formBuiltFor !== this._formState) {
        this._renderForm(t);
        this._formBuiltFor = this._formState;
      }
    } else {
      this._formBuiltFor = null;
      this._renderForm(t);
      this._renderPlansList(t);
    }
  }
}

customElements.define("truetemp-vacation-card", TrueTempVacationCard);

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "truetemp" } } },
];

// Thin wrapper around ha-form, same reasoning as TrueTempHolidayCardEditor.
class TrueTempVacationCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const t = STRINGS[pickLang(this._hass)];
    const labels = { entity: t.vacationEditorEntity };
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

customElements.define("truetemp-vacation-card-editor", TrueTempVacationCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "truetemp-vacation-card",
  name: "TrueTemp Vacation Plans",
  description: "Manage a list of recurring or one-off vacation setback plans and arm/disarm them.",
});
