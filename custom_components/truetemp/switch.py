"""Switch platform: the master compensation on/off."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TrueTempConfigEntry
from .coordinator import TrueTempCoordinator
from .sensor import TrueTempEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueTempConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ActiveSwitch(entry.runtime_data, entry),
            VacationModeSwitch(entry.runtime_data, entry),
        ]
    )


class ActiveSwitch(TrueTempEntity, SwitchEntity, RestoreEntity):
    """Master on/off for applying compensation.

    Off: the sensor publishes the raw outdoor temperature unmodified and the
    heat pump runs on its own curve, exactly as it did before this integration
    was installed. The controller keeps computing, and its recommendation stays
    readable on the main sensor's `recommended_compensated_outdoor_temp_c`
    attribute, so off is how you preview it.

    Defaults off: a fresh install computes and displays its recommendation
    without touching the heat pump until the occupant switches it on.

    The same flag is `hvac_mode` on the climate entity (AUTO/OFF). Both are
    views over `coordinator.is_active` rather than separate state, so they
    cannot disagree.
    """

    _attr_translation_key = "active"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_active

    @property
    def available(self) -> bool:
        """Always controllable: a local flag, not fetched data, so compensation
        can still be switched off while a source is unavailable."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self.coordinator.is_active = last_state.state == "on"
        # else: keep the coordinator's default (off).
        # The coordinator's first refresh already ran (before this entity
        # existed to restore anything) with default-derived values and may
        # have pushed them to hardware. Request a fresh cycle now so the
        # restored value reaches the output promptly rather than waiting up
        # to a full update interval. The debouncer coalesces this with the
        # other entities' restores into one refresh.
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.is_active = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.is_active = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class VacationModeSwitch(TrueTempEntity, SwitchEntity, RestoreEntity):
    """The one master kill switch over every vacation plan.

    Flipping it off suspends every plan regardless of their individual
    `enabled` flags, so "I'm home early" never requires editing N records —
    see §3 of docs/plan_vacation_plans.md. Unlike the old single-scenario
    switch, there is no coordinator-driven auto-disarm: a shared switch over
    N independent plans can't auto-disarm just because one `once` plan
    finished while others may still be scheduled (see the comment at the
    `resolve_vacation_with_return_ramp()` call site in coordinator.py).

    Defaults on: with no plans configured yet an armed-but-empty switch is a
    no-op, so a fresh install starts ready rather than needing a first-run
    flip once the occupant adds their first plan.
    """

    _attr_translation_key = "vacation_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_vacation_mode"

    @property
    def is_on(self) -> bool:
        return self.coordinator.vacation_armed

    @property
    def available(self) -> bool:
        """Always controllable: a local flag, not fetched data."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self.coordinator.vacation_armed = last_state.state == "on"
        # else: keep the coordinator's default (armed).
        # See ActiveSwitch.async_added_to_hass: the coordinator's first cycle
        # ran before this entity restored its value.
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.vacation_armed = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.vacation_armed = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
