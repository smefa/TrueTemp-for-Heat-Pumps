"""The TrueTemp integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONFIG_ENTRY_VERSION,
    DATA_VACATION_SKIP_RELOAD,
    DOMAIN,
    FRONTEND_CARD_URL,
    FRONTEND_DASHBOARD_JS_VERSION,
    FRONTEND_DASHBOARD_URL,
    FRONTEND_JS_VERSION,
    FRONTEND_STATIC_URL_PREFIX,
    FRONTEND_VACATION_CARD_URL,
    FRONTEND_VACATION_JS_VERSION,
    KNOWN_CONFIG_KEYS,
)
from .coordinator import TrueTempCoordinator
from .frontend_resources import async_remove_resource, async_sync_resource
from .services import async_register_services

PLATFORMS = [
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type TrueTempConfigEntry = ConfigEntry[TrueTempCoordinator]

_LOGGER = logging.getLogger(__name__)

_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"

# The three bundled scripts. Loaded through the Lovelace resources (like any
# HACS card) rather than the index page: on the index page they ran before the
# frontend was ready and were sometimes not found when a dashboard first drew.
_RESOURCE_URLS = (
    f"{FRONTEND_CARD_URL}?v={FRONTEND_JS_VERSION}",
    f"{FRONTEND_VACATION_CARD_URL}?v={FRONTEND_VACATION_JS_VERSION}",
    f"{FRONTEND_DASHBOARD_URL}?v={FRONTEND_DASHBOARD_JS_VERSION}",
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop stored settings that no version of this code reads any more.

    v0.2.0 deleted the RC estimator, the MPC planner, the hand-typed control
    gains and the drawn cold taper, but deleting the code that reads a key does
    not delete the key: the options flow saves
    `{**existing_options, **page_fields}`, so anything ever written is carried
    forward by every later edit. An entry created before v0.2.0 still holds
    around nineteen of those, which show up in diagnostics and in the raw entry
    and read as live configuration when they are inert.

    Pruning is by allow-list (`KNOWN_CONFIG_KEYS`) rather than by naming the
    dead keys, so this is correct for an entry from any older version without
    needing a table per version. Nothing is added: a key that is simply absent
    keeps falling back to its default, which is already how the code treats it.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer version than this code understands. Refusing is
        # the only safe move — HA will show the entry as needing a newer build
        # rather than us silently pruning keys we don't know about yet.
        return False

    stray = sorted((set(entry.data) | set(entry.options)) - KNOWN_CONFIG_KEYS)
    if stray:
        _LOGGER.info(
            "Removing %d superseded setting(s) from %s: %s",
            len(stray),
            entry.title,
            ", ".join(stray),
        )
    hass.config_entries.async_update_entry(
        entry,
        data={k: v for k, v in entry.data.items() if k in KNOWN_CONFIG_KEYS},
        options={k: v for k, v in entry.options.items() if k in KNOWN_CONFIG_KEYS},
        version=CONFIG_ENTRY_VERSION,
    )
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled cards and dashboard strategy.

    The whole www/ directory is registered, not just truetemp-card.js: the
    card `import`s its per-language strings from www/lang/*.js, and those
    files need to be reachable at their own URLs for that to resolve.

    The scripts are deliberately not added to the index page here: they are
    listed in the Lovelace resources instead (see _async_sync_resources), which
    loads them from inside the frontend once it is ready.

    Guarded by a hass.data flag rather than being tied to one config entry:
    a second TrueTemp zone would otherwise try to register the same
    static path twice, which raises.
    """
    if hass.data.get(_FRONTEND_REGISTERED):
        return
    hass.data[_FRONTEND_REGISTERED] = True
    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_STATIC_URL_PREFIX, str(www_path), cache_headers=True)]
    )


def _storage_resources(hass: HomeAssistant):
    """The Lovelace resources collection, or None when it isn't UI-managed.

    Read through hass.data rather than an import: Lovelace exposes no API for
    integrations to edit resources, and the shape of its hass.data entry has
    changed between releases. A YAML-mode collection has no create method.
    """
    resources = getattr(hass.data.get("lovelace"), "resources", None)
    return resources if hasattr(resources, "async_create_item") else None


def _add_to_index_page(hass: HomeAssistant) -> None:
    for url in _RESOURCE_URLS:
        add_extra_js_url(hass, url)


async def _async_sync_resources(hass: HomeAssistant) -> None:
    """List the bundled scripts in the Lovelace resources.

    Falls back to the index page (the behaviour before 0.4.9.1) when the
    resources can't be edited, so a YAML-mode setup still gets the scripts.
    """
    resources = _storage_resources(hass)
    if resources is None:
        _add_to_index_page(hass)
        _LOGGER.info(
            "Lovelace resources are managed in YAML, so TrueTemp can't list its scripts "
            "there. If the TrueTemp cards or dashboard fail to load at first, add these "
            "resources (type: module): %s",
            ", ".join(_RESOURCE_URLS),
        )
        return
    try:
        for url in _RESOURCE_URLS:
            result = await async_sync_resource(resources, url)
            if result:
                _LOGGER.info("Lovelace resource %s %s", url, result)
    except Exception:  # noqa: BLE001 - a resources problem must never block setup
        _LOGGER.warning(
            "Could not add the TrueTemp scripts to the Lovelace resources", exc_info=True
        )
        _add_to_index_page(hass)


async def async_setup_entry(hass: HomeAssistant, entry: TrueTempConfigEntry) -> bool:
    await _async_register_frontend(hass)
    # Outside the run-once guard above: the entry may have been removed with the
    # last zone, and a new zone added without a restart has to put it back.
    await _async_sync_resources(hass)
    async_register_services(hass)
    coordinator = TrueTempCoordinator(hass, entry)
    # Restore the learned offset table and lag buffer (if any) BEFORE the first
    # refresh, so the first cycle continues from prior learning rather than
    # from a cold start. Strictly additive: this never raises and cleanly cold
    # starts on empty, corrupt or version-mismatched state.
    await coordinator.async_load_state()
    # Deliberately async_refresh(), not async_config_entry_first_refresh():
    # the latter turns a failed first cycle (e.g. the outdoor sensor still
    # being unavailable while HA is starting up) into ConfigEntryNotReady,
    # which aborts setup before any entities are even created — the
    # integration then looks like it failed to load at all. async_refresh()
    # never raises; entities still get created below and report themselves
    # unavailable via CoordinatorEntity.available until a cycle succeeds,
    # which the coordinator keeps retrying on its normal schedule.
    await coordinator.async_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Don't rely solely on the coordinator's own polling interval to notice
    # a required/soft-degraded source recovering: react immediately to that
    # source's own state changes too. See
    # TrueTempCoordinator.watched_entity_ids for why.
    watched = coordinator.watched_entity_ids()
    if watched:

        async def _async_source_state_changed(
            event: Event[EventStateChangedData],
        ) -> None:
            await coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_state_change_event(hass, watched, _async_source_state_changed)
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: TrueTempConfigEntry) -> None:
    """Take the script resources out again when the last zone is deleted."""
    if any(e.entry_id != entry.entry_id for e in hass.config_entries.async_entries(DOMAIN)):
        return
    resources = _storage_resources(hass)
    if resources is None:
        return
    try:
        for url in _RESOURCE_URLS:
            await async_remove_resource(resources, url)
    except Exception:  # noqa: BLE001 - removal is cleanup only
        _LOGGER.warning("Could not remove the TrueTemp script resources", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: TrueTempConfigEntry) -> bool:
    # Flush any pending debounced write before teardown: HA only auto-flushes
    # Store.async_delay_save on full shutdown, not on an entry reload, so
    # without this the most recent learning is lost on every options change.
    await entry.runtime_data.async_save_state_now()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Otherwise a Repair raised while a source was down outlives the
    # integration that raised it (or the reload that briefly tears it down).
    entry.runtime_data.clear_source_issues()
    # Otherwise a flag set just before this unload (a vacation service call
    # racing a reload/removal) would sit in hass.data and be consumed by a
    # future entry that happens to reuse this entry_id's slot.
    skip_reload = hass.data.get(DATA_VACATION_SKIP_RELOAD)
    if skip_reload:
        skip_reload.discard(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: TrueTempConfigEntry) -> None:
    # A vacation_plan_set/_remove/_reorder service call (services.py) writes
    # entry.options the same way the options flow does, which normally means
    # a full unload/re-setup here — that briefly removes every TrueTemp
    # entity from the state machine, which is what made the vacation card go
    # blank for a few seconds on every plan add/edit/delete. But the plan
    # list is the one option TrueTempCoordinator.vacation_plans already
    # re-reads live from entry.options on each refresh (see its docstring),
    # so for that one key a plain refresh is enough — no reload, no entity
    # teardown. services.py flags the entry here right before its write;
    # any other options change (the options flow, a future setting) leaves
    # the flag unset and falls through to the normal reload.
    skip_reload = hass.data.get(DATA_VACATION_SKIP_RELOAD)
    if skip_reload and entry.entry_id in skip_reload:
        skip_reload.discard(entry.entry_id)
        await entry.runtime_data.async_request_refresh()
        return
    await hass.config_entries.async_reload(entry.entry_id)
