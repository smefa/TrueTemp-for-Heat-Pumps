// TrueTemp dashboard strategy.
//
// Registered automatically as a frontend resource at startup, like the cards.
// Home Assistant 2026.5+ lists it under Settings -> Dashboards -> Add dashboard
// -> Community dashboards -> "TrueTemp". Older versions can use it from YAML:
//     strategy:
//       type: custom:truetemp
//
// The dashboard is generated, not stored: it finds every TrueTemp zone through
// hass.entities (the frontend's compact entity registry, readable by non-admin
// users too) and gives each zone a view with the status card and the
// vacation-plans card, so a new zone shows up without editing anything.

// Registry translation_key of a zone's status sensor — see the note at the top
// of truetemp-card.js on why this is matched instead of the entity_id.
const STATUS_KEY = "status";

function zoneTitle(hass, entityId, info) {
  const device = hass.devices && info.device_id && hass.devices[info.device_id];
  const name = device && (device.name_by_user || device.name);
  return name || (hass.states[entityId] && hass.states[entityId].attributes.friendly_name) || entityId;
}

function findZones(hass) {
  const zones = [];
  Object.keys(hass.entities || {}).forEach((entityId) => {
    const info = hass.entities[entityId];
    if (info.platform !== "truetemp" || !entityId.startsWith("sensor.")) return;
    const isStatus = info.translation_key
      ? info.translation_key === STATUS_KEY
      : entityId.endsWith("_status") && !entityId.endsWith("_vacation_status");
    if (isStatus) zones.push({ entityId, title: zoneTitle(hass, entityId, info) });
  });
  return zones.sort((a, b) => a.title.localeCompare(b.title));
}

class TrueTempDashboardStrategy extends HTMLElement {
  static getCreateSuggestions(_hass) {
    return { title: "TrueTemp", icon: "mdi:thermometer-auto" };
  }

  static async generate(config, hass) {
    const zones = findZones(hass);
    if (!zones.length) {
      return {
        title: config.title || "TrueTemp",
        views: [
          {
            title: "TrueTemp",
            cards: [
              {
                type: "markdown",
                content: "No TrueTemp zone found. Add one under Settings -> Devices & services.",
              },
            ],
          },
        ],
      };
    }

    return {
      title: config.title || "TrueTemp",
      views: zones.map((zone) => ({
        title: zone.title,
        path: zone.entityId.replace(/^sensor\./, ""),
        icon: "mdi:thermometer-auto",
        cards: [
          { type: "custom:truetemp-card", entity: zone.entityId },
          { type: "custom:truetemp-vacation-card", entity: zone.entityId },
        ],
      })),
    };
  }
}

customElements.define("ll-strategy-dashboard-truetemp", TrueTempDashboardStrategy);

window.customStrategies = window.customStrategies || [];
window.customStrategies.push({
  type: "truetemp",
  strategyType: "dashboard",
  name: "TrueTemp",
  description: "One view per TrueTemp zone: what it is doing now, and your vacation plans.",
});
