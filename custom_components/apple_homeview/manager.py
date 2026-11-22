"""Connection manager for an Apple HomeView."""

from __future__ import annotations

import asyncio
import logging
from random import randrange
from typing import TYPE_CHECKING, Any

from pyatv import connect, exceptions, scan
from pyatv.conf import AppleTV as AppleTVConfig
from pyatv.const import Protocol
from pyatv.interface import AppleTV as AppleTVInterface, DeviceListener
from pyatv.settings import MrpTunnel
from pyatv.storage.memory_storage import MemoryStorage

from homeassistant.components import zeroconf
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CLIENT_NAME,
    CONF_CREDENTIALS,
    CONF_IDENTIFIER,
    CONF_MODEL,
    DOMAIN,
    SIGNAL_CONNECTED,
    SIGNAL_DISCONNECTED,
    model_name,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

DEFAULT_AIRPLAY_PORT = 7000
BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # five minutes
SCAN_TIMEOUT = 5

AUTH_EXCEPTIONS = (
    exceptions.AuthenticationError,
    exceptions.InvalidCredentialsError,
    exceptions.NoCredentialsError,
)
CONNECTION_EXCEPTIONS = (
    OSError,
    TimeoutError,
    exceptions.ConnectionLostError,
    exceptions.ConnectionFailedError,
    exceptions.ProtocolError,
    exceptions.NoServiceError,
    exceptions.BackOffError,
    exceptions.DeviceIdMissingError,
)


class DeviceNotFound(Exception):
    """The HomeView did not answer a scan."""


async def async_scan_device(
    hass: HomeAssistant,
    address: str | None = None,
    identifier: str | None = None,
    timeout: int = SCAN_TIMEOUT,
) -> list[AppleTVConfig]:
    """Scan for AirPlay devices using Home Assistant's shared zeroconf."""
    aiozc = await zeroconf.async_get_async_instance(hass)
    return list(
        await scan(
            hass.loop,
            timeout=timeout,
            identifier=identifier,
            protocol={Protocol.AirPlay, Protocol.RAOP},
            hosts=[address] if address else None,
            aiozc=aiozc,
        )
    )


async def async_connect_device(
    hass: HomeAssistant, conf: AppleTVConfig, credentials: str
) -> AppleTVInterface:
    """Connect to a HomeView with stored AirPlay credentials.

    pyatv only opens its remote control tunnel for models it recognises, so
    the tunnel is forced on for this connection. Nothing in pyatv is patched,
    which keeps Home Assistant's own Apple TV integration unaffected.
    """
    storage = MemoryStorage()
    settings = await storage.get_settings(conf)
    settings.info.name = CLIENT_NAME
    settings.protocols.airplay.mrp_tunnel = MrpTunnel.Force
    settings.protocols.airplay.credentials = credentials
    settings.protocols.raop.credentials = credentials
    return await connect(
        conf, hass.loop, session=async_get_clientsession(hass), storage=storage
    )


class HomeViewManager(DeviceListener):
    """Keep a connection open to one HomeView and share it with entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.atv: AppleTVInterface | None = None
        self.airplay_port: int = DEFAULT_AIRPLAY_PORT
        self._task: asyncio.Task[None] | None = None
        self._attempts = 0
        self._connection_was_lost = False
        self._stopping = False

    @property
    def unique_id(self) -> str:
        """Return the config entry unique id."""
        assert self.entry.unique_id is not None
        return self.entry.unique_id

    @property
    def name(self) -> str:
        """Return the device name."""
        return str(self.entry.data[CONF_NAME])

    @property
    def is_connecting(self) -> bool:
        """Return True while a background connect loop is running."""
        return self._task is not None and self.atv is None

    async def async_first_connect(self) -> None:
        """Connect once during setup; raise so setup can be retried."""
        await self._async_connect_once()

    @callback
    def async_start(self) -> None:
        """Make sure a connection exists or is being made."""
        self._stopping = False
        if self.atv is None and self._task is None:
            self._task = self.entry.async_create_background_task(
                self.hass,
                self._async_connect_loop(),
                name=f"{DOMAIN} connect loop {self.name}",
                eager_start=True,
            )

    async def async_stop(self) -> None:
        """Close the connection and stop reconnecting."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.atv is not None:
            atv, self.atv = self.atv, None
            atv.close()

    # pyatv DeviceListener callbacks

    def connection_lost(self, exception: Exception) -> None:
        """Handle an unexpected disconnect."""
        _LOGGER.warning("Connection lost to %s: %s", self.name, exception)
        self._connection_was_lost = True
        self._handle_disconnect()

    def connection_closed(self) -> None:
        """Handle an intentional disconnect."""
        self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        if self.atv is not None:
            atv, self.atv = self.atv, None
            atv.close()
        self._dispatch(SIGNAL_DISCONNECTED)
        if not self._stopping:
            self.async_start()

    # Internals

    async def _async_connect_loop(self) -> None:
        try:
            while not self._stopping and self.atv is None:
                try:
                    await self._async_connect_once()
                except AUTH_EXCEPTIONS:
                    _LOGGER.error(
                        "HomeView %s rejected the stored pairing; re-pair it", self.name
                    )
                    self.entry.async_start_reauth(self.hass)
                    return
                except (DeviceNotFound, *CONNECTION_EXCEPTIONS) as err:
                    _LOGGER.debug("Could not connect to %s: %s", self.name, err)
                except Exception:
                    _LOGGER.exception("Unexpected error connecting to %s", self.name)
                if self.atv is not None:
                    break
                self._attempts += 1
                delay = min(
                    max(BACKOFF_TIME_LOWER_LIMIT, randrange(2**self._attempts)),
                    BACKOFF_TIME_UPPER_LIMIT,
                )
                _LOGGER.debug("Reconnecting to %s in %d seconds", self.name, delay)
                await asyncio.sleep(delay)
        finally:
            self._task = None

    async def _async_connect_once(self) -> None:
        data = self.entry.data
        confs = await async_scan_device(
            self.hass, address=data[CONF_ADDRESS], identifier=data[CONF_IDENTIFIER]
        )
        if not confs:
            # The address may have changed; a broad scan finds it by identifier.
            confs = await async_scan_device(self.hass, identifier=data[CONF_IDENTIFIER])
        if not confs:
            raise DeviceNotFound(f"{self.name} not found on the network")

        conf = confs[0]
        service = conf.get_service(Protocol.AirPlay)
        if service is None:
            raise DeviceNotFound(f"{self.name} has no AirPlay service")
        self.airplay_port = service.port or DEFAULT_AIRPLAY_PORT

        atv = await async_connect_device(self.hass, conf, data[CONF_CREDENTIALS])
        atv.listener = self
        self.atv = atv
        self._attempts = 0
        if self._connection_was_lost:
            _LOGGER.info("Reconnected to %s", self.name)
            self._connection_was_lost = False

        address = str(conf.address)
        if address != data[CONF_ADDRESS]:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**data, CONF_ADDRESS: address}
            )
        self._update_device_registry(atv)
        self._dispatch(SIGNAL_CONNECTED, atv)

    @callback
    def _update_device_registry(self, atv: AppleTVInterface) -> None:
        info = atv.device_info
        raw_model = info.raw_model or self.entry.data.get(CONF_MODEL)
        device_registry = dr.async_get(self.hass)
        device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.unique_id)},
            connections=(
                {(dr.CONNECTION_NETWORK_MAC, dr.format_mac(info.mac))}
                if info.mac
                else set()
            ),
            manufacturer="Apple",
            model=model_name(raw_model),
            model_id=raw_model,
            name=self.name,
            sw_version=info.version,
        )

    def _dispatch(self, signal: str, *args: Any) -> None:
        async_dispatcher_send(self.hass, f"{signal}_{self.unique_id}", *args)
