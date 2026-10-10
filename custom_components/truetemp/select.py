"""Select platform: the two price-related preferences.

Both are live entities backed by in-memory coordinator values rather than
config options, for the reason given in coordinator.py: an options change
reloads the entry, and these are flipped often — by hand, by season, by
automation. Each restores its own value across restarts via RestoreEntity.

There is no longer a tuning-mode select. Manual and Auto were a choice between
hand-typed coefficients and coefficients derived from a model; there are no
coefficients to type any more, so there is nothing to choose between.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TrueTempConfigEntry
from .coordinator import TrueTempCoordinator
from .heuristic import (
    COLD_CAUTION_MID,
    COLD_CAUTIONS,
    PRICE_TIER_MID,
    PRICE_TIERS,
)
from .sensor import TrueTempEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueTempConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            PriceComfortTierSelect(entry.runtime_data, entry),
            ColdCautionSelect(entry.runtime_data, entry),
        ]
    )


class _LiveSelect(TrueTempEntity, SelectEntity, RestoreEntity):
    """A select over one in-memory coordinator field."""

    _field: str
    _fallback: str

    @property
    def available(self) -> bool:
        """Always settable: a local value, not fetched data. Overrides
        CoordinatorEntity's default so this still works while a source is down
        and the sensors show unavailable."""
        return True

    @property
    def current_option(self) -> str:
        return getattr(self.coordinator, self._field)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            setattr(self.coordinator, self._field, last_state.state)
        # The coordinator's first refresh ran before this entity existed to
        # restore anything, on default-derived values that may already have
        # been pushed to hardware. Request a fresh cycle now rather than
        # waiting up to a full update interval for the restored value to
        # reach the output.
        await self.coordinator.async_request_refresh()

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            option = self._fallback
        setattr(self.coordinator, self._field, option)
        self.async_write_ha_state()
        # Reflect the change promptly rather than waiting for the next cycle.
        await self.coordinator.async_request_refresh()


class PriceComfortTierSelect(_LiveSelect):
    """How much comfort to trade for cheaper energy.

    Low barely reacts (maximum comfort); High brakes hard on spikes and banks
    heat ahead of them (maximum saving). The tier sets how deep a sag is
    allowed and how sharply the response curves — it no longer carries a lead
    time, because how early to act is measured from the house rather than
    chosen (see lag.py).
    """

    _attr_translation_key = "price_comfort_tier"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(PRICE_TIERS)
    _field = "price_comfort_tier"
    _fallback = PRICE_TIER_MID

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_price_comfort_tier"


class ColdCautionSelect(_LiveSelect):
    """How careful to be about saving money when it is properly cold.

    Deliberately a manual choice rather than something derived. Braking is
    cheap to enter and expensive to exit: recovering a sag at -15 degC is slow,
    and on most installs it eventually runs through resistive backup heat at a
    terrible coefficient of performance. Whether that trade is worth making is
    a question about money versus comfort, not a measurable fact about the
    building — so it stays the occupant's to answer.

    Low stops braking only below -20 degC, Medium below -10 degC, High below
    -5 degC, each ramping back to full authority over the degrees above that.
    On top of the setting, once a temperature band has been observed long
    enough to know how fast it recovers, the sag is additionally limited to
    what can actually be bought back.
    """

    _attr_translation_key = "cold_caution"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(COLD_CAUTIONS)
    _field = "cold_caution"
    _fallback = COLD_CAUTION_MID

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_cold_caution"
