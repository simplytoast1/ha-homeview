"""The Apple HomeView integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .manager import (
    AUTH_EXCEPTIONS,
    CONNECTION_EXCEPTIONS,
    DeviceNotFound,
    HomeViewManager,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.REMOTE]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_SPEAKERS = "speakers"
ATTR_SPEAKER = "speaker"
ATTR_VOLUME_LEVEL = "volume_level"

SPEAKER_LIST_SCHEMA = {
    vol.Required(ATTR_SPEAKERS): vol.All(cv.ensure_list, [cv.string]),
}
SPEAKER_VOLUME_SCHEMA = {
    vol.Required(ATTR_SPEAKER): cv.string,
    vol.Required(ATTR_VOLUME_LEVEL): vol.All(vol.Coerce(float), vol.Range(0, 1)),
}

type HomeViewConfigEntry = ConfigEntry[HomeViewManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the AirPlay speaker actions."""
    for name, schema, func in (
        ("add_speakers", SPEAKER_LIST_SCHEMA, "async_add_speakers"),
        ("remove_speakers", SPEAKER_LIST_SCHEMA, "async_remove_speakers"),
        ("set_speakers", SPEAKER_LIST_SCHEMA, "async_set_speakers"),
        ("set_speaker_volume", SPEAKER_VOLUME_SCHEMA, "async_set_speaker_volume"),
    ):
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=MEDIA_PLAYER_DOMAIN,
            schema=schema,
            func=func,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomeViewConfigEntry) -> bool:
    """Set up a HomeView from a config entry."""
    manager = HomeViewManager(hass, entry)
    address = entry.data[CONF_ADDRESS]

    try:
        await manager.async_first_connect()
    except AUTH_EXCEPTIONS as err:
        await manager.async_stop()
        raise ConfigEntryAuthFailed(
            f"{address}: pairing was rejected, re-pair the HomeView: {err}"
        ) from err
    except (DeviceNotFound, *CONNECTION_EXCEPTIONS) as err:
        await manager.async_stop()
        raise ConfigEntryNotReady(f"{address}: {err}") from err

    entry.runtime_data = manager

    async def _on_stop(event: Event) -> None:
        await manager.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    )
    entry.async_on_unload(manager.async_stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HomeViewConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
