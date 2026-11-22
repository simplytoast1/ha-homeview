"""Base entity for Apple HomeView."""

from __future__ import annotations

from pyatv.const import FeatureName, FeatureState
from pyatv.interface import AppleTV as AppleTVInterface

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_CONNECTED, SIGNAL_DISCONNECTED
from .manager import HomeViewManager


class HomeViewEntity(Entity):
    """Entity backed by a HomeView connection."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, manager: HomeViewManager, key: str | None = None) -> None:
        """Initialize the entity."""
        self.manager = manager
        self.atv: AppleTVInterface | None = None
        uid = manager.unique_id
        self._attr_unique_id = f"{uid}_{key}" if key else uid
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)}, name=manager.name
        )

    @property
    def available(self) -> bool:
        """Entity is available while connected."""
        return self.atv is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to connection changes."""
        uid = self.manager.unique_id

        @callback
        def _connected(atv: AppleTVInterface) -> None:
            self.atv = atv
            self.async_device_connected(atv)
            self.async_write_ha_state()

        @callback
        def _disconnected() -> None:
            self.async_device_disconnected()
            self.atv = None
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"{SIGNAL_CONNECTED}_{uid}", _connected)
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_DISCONNECTED}_{uid}", _disconnected
            )
        )
        if self.manager.atv is not None:
            _connected(self.manager.atv)

    @callback
    def async_device_connected(self, atv: AppleTVInterface) -> None:
        """Run when a connection is established."""

    @callback
    def async_device_disconnected(self) -> None:
        """Run when the connection is lost."""

    def _is_available(self, feature: FeatureName) -> bool:
        """Return True if the connected device currently offers a feature."""
        return self.atv is not None and self.atv.features.in_state(
            FeatureState.Available, feature
        )
